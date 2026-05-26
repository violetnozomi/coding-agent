"""Persistent Memory（第二层）：跨 session 的持久化知识库。

三层记忆架构中的第二层：
  - 第一层: Scratchpad（session-scoped，nz_coder/tools/scratchpad.py）
  - 第二层: Memory（磁盘持久化，本文件）
  - 第三层: Context（当前对话历史，nz_coder/context.py）

改进点（对标 Claude Code memdir）：
  - MEMORY.md 索引增加行数（200行）和字节（25KB）双上限保护
  - build_prompt_block 支持 query 参数，按当前用户消息做相关性过滤
  - 索引格式升级为 - [name](file.md) — description [type]
  - recall 结果附带完整 content，而不是只返回 header
  - 新增 scan_headers() 用于快速索引扫描（不加载 content）
  - 新增 load_content() 按需加载单条 memory 正文
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from nz_coder import config
from nz_coder.tools import register

MEMORY_TYPES = ("user", "project", "feedback", "reference")

_COVERAGE_WEIGHT = 0.55
_JACCARD_WEIGHT = 0.20
_EXACT_WEIGHT = 0.15
_FRESHNESS_WEIGHT = 0.10
_SIMILARITY_MERGE_THRESHOLD = 0.72
_SIMILARITY_WARN_THRESHOLD = 0.58
_CONTENT_REDUNDANT_THRESHOLD = 0.92
_SIMILARITY_MIN_TOKENS_FOR_MERGE = 5
_USER_MEMORY_ALWAYS_INCLUDE = 3
_CLEANUP_DAYS_DEFAULT = 30

# MEMORY.md 索引保护上限（对标 Claude Code memdir.ts）
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000


class MemoryManager:
    """跨 session 的持久化知识库管理器。"""

    def __init__(self, memory_dir: Path = None) -> None:
        self.memory_dir = memory_dir or config.MEMORY_DIR
        self.memories: dict[str, dict] = {}

    # ── 加载 ─────────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """从磁盘扫描并加载所有 memory 文件（header + content）。"""
        self.memories = {}
        if not self.memory_dir.exists():
            return
        for md in sorted(self.memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "created_at": float(parsed.get("created_at", 0.0)),
                    "last_accessed": float(parsed.get("last_accessed", 0.0)),
                    "access_count": int(parsed.get("access_count", 0)),
                    "file": md.name,
                }

    def scan_headers(self) -> list[dict]:
        """返回所有 memory 的 header（不含正文），用于索引展示。

        对标 Claude Code scanMemoryFiles()：只读 frontmatter，
        正文按需通过 load_content() 加载。
        """
        headers = []
        for name, mem in self.memories.items():
            headers.append({
                "name": name,
                "description": mem["description"],
                "type": mem["type"],
                "last_accessed": mem["last_accessed"],
                "file": mem.get("file", f"{self._safe_name(name)}.md"),
            })
        return sorted(headers, key=lambda h: h["last_accessed"], reverse=True)

    # ── recall ───────────────────────────────────────────────────────────────

    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """按多信号相关性排序返回 memories（含完整 content）。

        评分结合代码感知 token 覆盖率、Jaccard、精确短语命中和新鲜度。
        新鲜度只作为相关结果的微调，不再让无关 memory 仅凭访问时间进入结果。
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        now = time.time()
        scored: list[tuple[float, str, dict]] = []
        for name, mem in self.memories.items():
            text = _memory_text(name, mem)
            score = _relevance_score(query, query_tokens, text, mem, now)
            if score > 0:
                scored.append((score, name, mem))

        scored.sort(key=lambda t: t[0], reverse=True)
        results = []
        for _, name, mem in scored[:top_k]:
            self._touch(name)
            results.append({"name": name, **mem})
        return results

    # ── build_prompt_block ───────────────────────────────────────────────────

    def build_prompt_block(
        self,
        query: Optional[str] = None,
        max_items: int = 8,
        max_chars: int = 3000,
        rerank_client=None,
        model: str | None = None,
    ) -> str:
        """生成注入 system prompt 的记忆块。

        - user 类型偏好始终优先注入少量条目
        - query 不为空时，事实类 memory 按相关性过滤
        - 可选 rerank_client 用一次 LLM 调用对粗召回结果重排
        """
        if not self.memories:
            return ""

        max_items = max(1, int(max_items or 8))
        user_items = self._user_preference_items(max_items)
        remaining_slots = max(0, max_items - len(user_items))

        if query and remaining_slots:
            candidates = self.recall(query, top_k=max(remaining_slots * 3, remaining_slots))
            candidates = [item for item in candidates if item.get("type") != "user"]
            candidates = rerank_memories(query, candidates, rerank_client, model, remaining_slots)
            items = user_items + candidates[:remaining_slots]
        elif remaining_slots:
            sorted_mems = sorted(
                self.memories.items(),
                key=lambda kv: kv[1]["last_accessed"],
                reverse=True,
            )
            seen = {item["name"] for item in user_items}
            recent = [
                {"name": n, **m}
                for n, m in sorted_mems
                if n not in seen
            ]
            items = user_items + recent[:remaining_slots]
        else:
            items = user_items

        if not items:
            return ""

        sections = [
            "## Memories (persistent across sessions)",
            "These are background notes. Current conversation instructions always take priority.",
            "User preference memories are durable defaults; task-specific instructions override them.",
            "Use `recall_memory` to retrieve relevant memories by query.",
            "",
        ]
        total_chars = sum(len(s) + 1 for s in sections)
        added = 0
        for item in items:
            fname = item.get("file", f"{self._safe_name(item['name'])}.md")
            line = f"- [{item['name']}]({fname}) — {item['description']} [{item['type']}]"
            if total_chars + len(line) + 1 > max_chars:
                break
            sections.append(line)
            total_chars += len(line) + 1
            added += 1

        remaining = len(items) - added
        if remaining > 0:
            sections.append(f"_(+{remaining} more — use `recall_memory` to search)_")

        sections.append("")
        return "\n".join(sections)

    # ── save ─────────────────────────────────────────────────────────────────

    def save(self, name: str, description: str, mem_type: str, content: str) -> str:
        """保存或更新一条 memory，同时重建 MEMORY.md 索引。

        新内容与已有条目高度相似时合并，避免 MEMORY.md 被重复事实膨胀。
        """
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        safe_name = self._safe_name(name)
        if not safe_name:
            return "Error: invalid memory name"

        now = time.time()
        existing_entry = self.memories.get(name)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        if existing_entry is None:
            merge_target = self._find_merge_target(name, description, mem_type, content)
            if merge_target:
                target_name, target_mem = merge_target
                merged = self._merge_memory(target_name, target_mem, description, content, now)
                self._write_memory_file(target_name, merged)
                self.memories[target_name] = merged
                self._rebuild_index()
                return f"Merged memory '{name}' into existing memory '{target_name}' [{mem_type}]"

        created_at = existing_entry["created_at"] if existing_entry else now
        fname = f"{safe_name}.md"
        fp = self.memory_dir / fname
        is_rename = fp.exists() and name not in self.memories

        for existing in list(self.memories):
            if self._safe_name(existing) == safe_name and existing != name:
                del self.memories[existing]

        entry = {
            "description": description,
            "type": mem_type,
            "content": content,
            "created_at": created_at,
            "last_accessed": now,
            "access_count": existing_entry["access_count"] if existing_entry else 0,
            "file": fname,
        }
        self._write_memory_file(name, entry)
        self.memories[name] = entry
        self._rebuild_index()

        if is_rename:
            return f"Updated existing memory '{name}' [{mem_type}]"
        action = "Updated" if existing_entry else "Saved"
        return f"{action} memory '{name}' [{mem_type}]"

    def _write_memory_file(self, name: str, mem: dict) -> None:
        """把单条 memory 写回 markdown 文件。"""
        fname = mem.get("file", f"{self._safe_name(name)}.md")
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {mem['description']}\n"
            f"type: {mem['type']}\n"
            f"created_at: {mem['created_at']}\n"
            f"last_accessed: {mem['last_accessed']}\n"
            f"access_count: {mem['access_count']}\n"
            f"---\n{mem['content']}\n"
        )
        (self.memory_dir / fname).write_text(frontmatter, encoding="utf-8")

    def _find_merge_target(
        self,
        name: str,
        description: str,
        mem_type: str,
        content: str,
    ) -> tuple[str, dict] | None:
        """查找可合并的近重复 memory。"""
        new_text = f"{name} {description} {content}"
        new_hash = _normalized_memory_text(description, content)
        best: tuple[float, str, dict] | None = None
        for ex_name, ex_mem in self.memories.items():
            if ex_mem.get("type") != mem_type:
                continue
            existing_hash = _normalized_memory_text(ex_mem["description"], ex_mem["content"])
            if new_hash and new_hash == existing_hash:
                return ex_name, ex_mem
            score = _memory_similarity(
                new_text,
                _memory_text(ex_name, ex_mem),
                min_tokens=_SIMILARITY_MIN_TOKENS_FOR_MERGE,
            )
            if score > (best[0] if best else 0.0):
                best = (score, ex_name, ex_mem)
        if best and best[0] >= _SIMILARITY_MERGE_THRESHOLD:
            return best[1], best[2]
        return None

    def _merge_memory(
        self,
        name: str,
        existing: dict,
        description: str,
        content: str,
        now: float,
    ) -> dict:
        """合并相似 memory，保留原创建时间和访问计数。"""
        merged_description = existing["description"]
        if _memory_similarity(merged_description, description) < _SIMILARITY_WARN_THRESHOLD:
            merged_description = _join_description(merged_description, description)

        merged_content = existing["content"]
        if not _is_redundant_text(content, merged_content):
            merged_content = f"{merged_content.rstrip()}\n\nAdditional note:\n{content.strip()}"

        return {
            "description": merged_description,
            "type": existing["type"],
            "content": merged_content,
            "created_at": existing["created_at"],
            "last_accessed": now,
            "access_count": existing["access_count"],
            "file": existing.get("file", f"{self._safe_name(name)}.md"),
        }

    def _user_preference_items(self, max_items: int) -> list[dict]:
        """返回需要始终注入的用户偏好 memory。"""
        limit = min(max_items, _USER_MEMORY_ALWAYS_INCLUDE)
        if limit <= 0:
            return []
        user_mems = [
            (name, mem)
            for name, mem in self.memories.items()
            if mem.get("type") == "user"
        ]
        user_mems.sort(key=lambda kv: kv[1]["last_accessed"], reverse=True)
        return [{"name": name, **mem} for name, mem in user_mems[:limit]]

    # ── list ─────────────────────────────────────────────────────────────────

    def list_memories(self, mem_type: str = None) -> str:
        """列出所有 memory，可按类型过滤。"""
        if mem_type and mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        if not self.memories:
            return "No memories saved."
        lines = ["# Memories", ""]
        for name, mem in sorted(self.memories.items()):
            if mem_type and mem["type"] != mem_type:
                continue
            fname = mem.get("file", f"{self._safe_name(name)}.md")
            lines.append(f"- [{name}]({fname}) — {mem['description']} [{mem['type']}]")
        return "\n".join(lines) if len(lines) > 2 else f"No memories of type '{mem_type}'."

    # ── delete ────────────────────────────────────────────────────────────────

    def delete(self, name: str) -> str:
        """删除指定 memory（内存 + 磁盘）。"""
        safe_name = self._safe_name(name)
        if not safe_name:
            return "Error: invalid memory name"
        fp = self.memory_dir / f"{safe_name}.md"
        removed = []
        for existing in list(self.memories):
            if existing == name or self._safe_name(existing) == safe_name:
                removed.append(existing)
                del self.memories[existing]
        existed_on_disk = fp.exists()
        if existed_on_disk:
            fp.unlink()
        if not removed and not existed_on_disk:
            return f"Error: memory not found: {name}"
        self._rebuild_index()
        label = ", ".join(removed) if removed else name
        return f"Deleted memory: {label}"

    # ── cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, days_threshold: int = _CLEANUP_DAYS_DEFAULT) -> str:
        """清理过期且从未被访问过的 memory。"""
        now = time.time()
        threshold_secs = days_threshold * 86400
        deleted = []
        staled = []

        for name, mem in list(self.memories.items()):
            age_secs = now - mem["created_at"]
            idle_secs = now - mem["last_accessed"] if mem["last_accessed"] else age_secs
            if mem["access_count"] == 0 and age_secs > threshold_secs:
                self.delete(name)
                deleted.append(name)
            elif mem["access_count"] > 0 and idle_secs > threshold_secs:
                if not mem["description"].endswith("[stale]"):
                    staled.append(name)
                    self.memories[name]["description"] += " [stale]"
                    self.save(name, self.memories[name]["description"], mem["type"], mem["content"])

        parts = []
        if deleted:
            parts.append(f"Deleted {len(deleted)} unused: {', '.join(deleted)}")
        if staled:
            parts.append(f"Marked {len(staled)} stale: {', '.join(staled)}")
        return "\n".join(parts) if parts else "Nothing to clean up."

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _touch(self, name: str) -> None:
        """更新 last_accessed 和 access_count（内存+磁盘）。"""
        if name not in self.memories:
            return
        mem = self.memories[name]
        mem["last_accessed"] = time.time()
        mem["access_count"] = mem["access_count"] + 1
        safe_name = self._safe_name(name)
        fp = self.memory_dir / f"{safe_name}.md"
        if fp.exists():
            frontmatter = (
                f"---\n"
                f"name: {name}\n"
                f"description: {mem['description']}\n"
                f"type: {mem['type']}\n"
                f"created_at: {mem['created_at']}\n"
                f"last_accessed: {mem['last_accessed']}\n"
                f"access_count: {mem['access_count']}\n"
                f"---\n{mem['content']}\n"
            )
            fp.write_text(frontmatter, encoding="utf-8")

    def _rebuild_index(self) -> None:
        """重建 MEMORY.md 索引文件，并执行行数/字节双上限保护。

        对标 Claude Code truncateEntrypointContent()：
        - 超过 MAX_INDEX_LINES 行：截断并追加警告
        - 超过 MAX_INDEX_BYTES 字节：在最后一个换行处截断并追加警告
        """
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # 按 last_accessed 降序排列，最近访问的先出现
        sorted_mems = sorted(
            self.memories.items(),
            key=lambda kv: kv[1]["last_accessed"],
            reverse=True,
        )

        lines = ["# Memory Index", ""]
        for name, mem in sorted_mems:
            fname = mem.get("file", f"{self._safe_name(name)}.md")
            lines.append(f"- [{name}]({fname}) — {mem['description']} [{mem['type']}]")

        content = "\n".join(lines) + "\n"

        # 行数上限保护
        all_lines = content.splitlines()
        if len(all_lines) > MAX_INDEX_LINES:
            truncated = "\n".join(all_lines[:MAX_INDEX_LINES])
            over = len(all_lines) - MAX_INDEX_LINES
            truncated += (
                f"\n\n> WARNING: MEMORY.md has {len(all_lines)} lines (limit: {MAX_INDEX_LINES}). "
                f"{over} entries omitted. Use `recall_memory` to search all memories."
            )
            content = truncated + "\n"

        # 字节上限保护
        if len(content.encode("utf-8")) > MAX_INDEX_BYTES:
            cut = content.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")
            last_nl = cut.rfind("\n")
            cut = cut[:last_nl] if last_nl > 0 else cut
            content = (
                cut
                + "\n\n> WARNING: MEMORY.md exceeds byte limit. "
                "Some entries were cut. Keep descriptions concise.\n"
            )

        index_path = self.memory_dir / "MEMORY.md"
        index_path.write_text(content, encoding="utf-8")

    def _parse_frontmatter(self, text: str) -> Optional[dict]:
        """解析 markdown frontmatter（--- ... ---）。"""
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not m:
            return None
        result: dict = {"content": m.group(2).strip()}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result

    def _safe_name(self, name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name).lower()).strip("_")


# ── 独立函数 ──────────────────────────────────────────────────────────────────

def extract_session_learnings(
    messages: list[dict],
    client=None,
    model: str | None = None,
) -> list[dict]:
    """从对话历史中提取值得跨 session 保存的经验。

    默认使用规则提取，传入 client/model 时追加一次 LLM 结构化事实提取。
    LLM 调用失败时返回规则提取结果，不影响 agent 主流程。
    """
    candidates = _extract_rule_based_learnings(messages)
    if client is not None and model:
        candidates.extend(_extract_llm_session_learnings(messages, client, model))
    return _dedupe_learning_candidates(candidates)


def _extract_rule_based_learnings(messages: list[dict]) -> list[dict]:
    """从显式触发词和重复失败中提取候选 memory。"""
    candidates: list[dict] = []
    trigger_patterns = [
        r"(?i)\bremember\s+that\b",
        r"(?i)\bnote\s+that\b",
        r"记住[：:]?\s*",
        r"需要记住[：:]?\s*",
    ]
    seen_names: set[str] = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        for pat in trigger_patterns:
            m = re.search(pat, content)
            if m:
                remainder = content[m.end():].strip()[:200]
                if not remainder:
                    continue
                raw_name = re.sub(r"[^a-z0-9_\u4e00-\u9fff]", "_", remainder[:30].lower())
                name = f"session_{raw_name}".strip("_")
                if name not in seen_names:
                    seen_names.add(name)
                    candidates.append({
                        "name": name,
                        "description": remainder[:80],
                        "type": "feedback",
                        "content": remainder,
                    })
                break

    fail_counter: Counter = Counter()
    fail_examples: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "tool":
            c = msg.get("content", "")
            if not isinstance(c, str):
                continue
            if c.startswith("Command exited with code"):
                m2 = re.search(r"FAILED\s+([\w/\.\-]+(?:::\w+)+)", c)
                if m2:
                    key = f"test_fail::{m2.group(1)}"
                else:
                    em = re.search(r"(\w+Error|\w+Exception):", c)
                    key = f"cmd_fail::{em.group(1)}" if em else f"cmd_fail::{c[25:60]}"
                fail_counter[key] += 1
                fail_examples.setdefault(key, c[:200])
            elif c.startswith("Error:"):
                key = re.sub(r"[^a-z0-9_]", "_", c[:60].lower())
                fail_counter[key] += 1
                fail_examples.setdefault(key, c[:200])
    for key, count in fail_counter.items():
        if count >= 2:
            safe_key = re.sub(r"[^a-z0-9]", "_", key[:40].lower())
            name = f"repeated_failure_{safe_key}"
            if name not in seen_names:
                seen_names.add(name)
                candidates.append({
                    "name": name,
                    "description": f"Repeated failure (x{count}): {key[:80]}",
                    "type": "feedback",
                    "content": (
                        f"This pattern occurred {count} times in one session.\n"
                        f"Key: {key}\n"
                        f"Example: {fail_examples.get(key, '')}"
                    ),
                })
    return candidates


def _extract_llm_session_learnings(messages: list[dict], client, model: str) -> list[dict]:
    """用 LLM 从最近对话中提取结构化长期记忆候选。"""
    excerpt = _conversation_excerpt(messages)
    if not excerpt:
        return []
    prompt = (
        "Extract durable cross-session memories for a coding agent. "
        "Return only a JSON object with key `memories`, whose value is a list "
        "of objects with fields name, description, type, content. Type must be one of: "
        f"{', '.join(MEMORY_TYPES)}. "
        "Keep only stable user preferences, project facts, repeated feedback, "
        "or reusable references. Exclude temporary task state, guesses, and tool noise. "
        "Limit to at most 5 items.\n\nConversation excerpt:\n"
        f"{excerpt}"
    )
    try:
        resp = _create_chat_completion(
            client,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        data = _parse_json_list(raw)
    except Exception:
        return []

    candidates: list[dict] = []
    for item in data[:5]:
        if not isinstance(item, dict):
            continue
        mem_type = str(item.get("type", "project")).strip()
        if mem_type not in MEMORY_TYPES:
            mem_type = "project"
        name = _candidate_name(str(item.get("name", "memory")))
        description = _clean_one_line(str(item.get("description", "")))[:160]
        content = str(item.get("content", "")).strip()[:1200]
        if name and description and content:
            candidates.append({
                "name": name,
                "description": description,
                "type": mem_type,
                "content": content,
            })
    return candidates


def _dedupe_learning_candidates(candidates: list[dict]) -> list[dict]:
    """候选列表内去重，保留先出现的高信号条目。"""
    deduped: list[dict] = []
    for item in candidates:
        if item.get("type") not in MEMORY_TYPES:
            continue
        text = f"{item.get('description', '')} {item.get('content', '')}"
        if any(_memory_similarity(text, f"{old['description']} {old['content']}") >= _SIMILARITY_MERGE_THRESHOLD for old in deduped):
            continue
        deduped.append(item)
    return deduped


# ── 模块级辅助函数 ────────────────────────────────────────────────────────────

_TOKEN_ALIASES = {
    "parsing": "parse",
    "parsed": "parse",
    "parser": "parse",
    "timezone": "tz",
    "timezones": "tz",
    "日期": "date",
    "解析": "parse",
    "时区": "timezone",
}

_SIMILARITY_STOPWORDS = {
    "a", "an", "and", "are", "as", "be", "for", "in", "is", "it",
    "avoid", "of", "on", "or", "prefer", "that", "the", "thi",
    "this", "to", "use", "used", "uses", "using", "with",
}


def _tokenize(text: str) -> set[str]:
    """代码感知 token 化：保留原词，同时拆 snake/camel/path 并做轻量归一。"""
    raw_words = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*|\d+|[\u4e00-\u9fff]+", text.lower())
    words: list[str] = []
    for raw in raw_words:
        pieces = _split_code_token(raw)
        if raw not in pieces:
            pieces.append(raw)
        for piece in pieces:
            normalized = _normalize_token(piece)
            if len(normalized) < 2 and not normalized.isdigit():
                continue
            words.append(normalized)
            alias = _TOKEN_ALIASES.get(normalized)
            if alias:
                words.append(alias)

    for phrase, alias in _TOKEN_ALIASES.items():
        if phrase in text.lower():
            words.append(alias)

    tokens: set[str] = set(words)
    for i in range(len(words) - 1):
        tokens.add(f"{words[i]}_{words[i+1]}")
    return tokens


def _split_code_token(token: str) -> list[str]:
    token = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", token)
    return [p for p in re.split(r"[_.\-/]+", token.lower()) if p]


def _normalize_token(token: str) -> str:
    token = token.strip("_.-/").lower()
    if token in _TOKEN_ALIASES:
        return _TOKEN_ALIASES[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 5:
        base = token[:-3]
        if base.endswith("s"):
            return base + "e"
        return base
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _memory_text(name: str, mem: dict) -> str:
    return f"{name} {mem.get('description', '')} {mem.get('content', '')} {mem.get('file', '')}"


def _relevance_score(
    query: str,
    query_tokens: set[str],
    text: str,
    mem: dict,
    now: float,
) -> float:
    mem_tokens = _tokenize(text)
    if not mem_tokens:
        return 0.0

    intersection = query_tokens & mem_tokens
    if not intersection:
        return 0.0

    coverage = len(intersection) / len(query_tokens)
    jaccard = _jaccard(query_tokens, mem_tokens)
    exact = _exact_phrase_score(query, text)
    freshness = _freshness(mem.get("last_accessed", 0.0), now)
    return (
        coverage * _COVERAGE_WEIGHT
        + jaccard * _JACCARD_WEIGHT
        + exact * _EXACT_WEIGHT
        + freshness * _FRESHNESS_WEIGHT
    )


def _exact_phrase_score(query: str, text: str) -> float:
    query_norm = _normalize_space(query.lower())
    text_norm = _normalize_space(text.lower())
    if query_norm and query_norm in text_norm:
        return 1.0
    query_identifiers = re.findall(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*", query.lower())
    if any(identifier in text_norm for identifier in query_identifiers):
        return 0.8
    return 0.0


def _memory_similarity(a: str, b: str, min_tokens: int = 0) -> float:
    a_tokens = _important_similarity_tokens(a)
    b_tokens = _important_similarity_tokens(b)
    if not a_tokens or not b_tokens:
        return 0.0
    if min_tokens and (len(a_tokens) < min_tokens or len(b_tokens) < min_tokens):
        return 0.0
    overlap = len(a_tokens & b_tokens)
    min_coverage = overlap / min(len(a_tokens), len(b_tokens))
    return max(_jaccard(a_tokens, b_tokens), min_coverage)


def _important_similarity_tokens(text: str) -> set[str]:
    return {
        token
        for token in _tokenize(text)
        if "_" not in token
        and token not in _SIMILARITY_STOPWORDS
    }


def _normalized_memory_text(description: str, content: str) -> str:
    return _normalize_space(f"{description} {content}".lower())


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_redundant_text(new_text: str, existing_text: str) -> bool:
    new_norm = _normalized_memory_text("", new_text)
    existing_norm = _normalized_memory_text("", existing_text)
    return bool(new_norm and (new_norm in existing_norm or _memory_similarity(new_text, existing_text) >= _CONTENT_REDUNDANT_THRESHOLD))


def _join_description(old: str, new: str, max_len: int = 180) -> str:
    if _normalize_space(new.lower()) in _normalize_space(old.lower()):
        return old
    joined = f"{old}; {new}"
    return joined[: max_len - 3].rstrip() + "..." if len(joined) > max_len else joined


def _create_chat_completion(client, **kwargs):
    """优先使用 JSON mode；不支持时回退到普通 completion。"""
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        if "response_format" not in kwargs:
            raise
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("response_format", None)
        return client.chat.completions.create(**fallback_kwargs)


def _parse_json_list(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\])", raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("memories", []) or data.get("items", [])
    return data if isinstance(data, list) else []


def _conversation_excerpt(messages: list[dict], max_chars: int = 12000) -> str:
    rows: list[str] = []
    budget = max_chars
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        line = f"{role}: {content.strip()}"
        if len(line) > 2000:
            line = line[:2000] + "..."
        if len(line) + 1 > budget:
            break
        rows.append(line)
        budget -= len(line) + 1
    rows.reverse()
    return "\n".join(rows)


def _candidate_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", name.strip().lower())
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:80]


def _clean_one_line(text: str) -> str:
    return _normalize_space(text.replace("\n", " "))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _freshness(last_accessed: float, now: float, half_life_days: float = 30.0) -> float:
    if last_accessed <= 0:
        return 0.0
    elapsed_days = (now - last_accessed) / 86400.0
    return math.exp(-math.log(2) * elapsed_days / half_life_days)


def rerank_memories(
    query: str,
    candidates: list[dict],
    client=None,
    model: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """用可选 LLM 对粗召回结果重排；失败时保持原排序。"""
    if not candidates or client is None or not model:
        return candidates[:top_k]

    payload = [
        {
            "name": item["name"],
            "type": item["type"],
            "description": item["description"],
            "content_preview": item.get("content", "")[:500],
        }
        for item in candidates[:10]
    ]
    prompt = (
        "Rerank these coding-agent memories for the query. "
        "Return only JSON: a list of memory names, most relevant first. "
        f"Limit to {top_k}.\n"
        f"Query: {query}\n"
        f"Memories: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        raw = resp.choices[0].message.content or ""
        names = _parse_json_list(raw)
    except Exception:
        return candidates[:top_k]

    by_name = {item["name"]: item for item in candidates}
    ranked: list[dict] = []
    for name in names:
        if isinstance(name, dict):
            name = name.get("name")
        if isinstance(name, str) and name in by_name and name not in {item["name"] for item in ranked}:
            ranked.append(by_name[name])
        if len(ranked) >= top_k:
            break
    for item in candidates:
        if len(ranked) >= top_k:
            break
        if item["name"] not in {r["name"] for r in ranked}:
            ranked.append(item)
    return ranked


# ── 全局实例 ──────────────────────────────────────────────────────────────────

memory_mgr = MemoryManager()


# ── 工具处理函数 ──────────────────────────────────────────────────────────────

def _save_memory(name: str, description: str, type: str, content: str) -> str:
    return memory_mgr.save(name, description, type, content)


def _list_memories(type: str = None) -> str:
    return memory_mgr.list_memories(type)


def _delete_memory(name: str) -> str:
    return memory_mgr.delete(name)


def _recall_memory(query: str, top_k: int = 5) -> str:
    results = memory_mgr.recall(query, top_k)
    if not results:
        return "No relevant memories found."
    lines = [f"Found {len(results)} relevant memory(ies):", ""]
    for r in results:
        lines.append(f"### {r['name']} [{r['type']}]")
        lines.append(f"Description: {r['description']}")
        lines.append(r["content"])
        lines.append("")
    return "\n".join(lines)


# ── 工具注册 ──────────────────────────────────────────────────────────────────

register(
    name="save_memory",
    description=(
        "Save cross-session information (user preferences, project facts, repeated feedback). "
        "Not for temporary state. Two-step: write the memory file, then the index is auto-updated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short identifier (e.g. 'user_role', 'feedback_testing')."},
            "description": {"type": "string", "description": "One-line summary shown in the index."},
            "type": {"type": "string", "enum": list(MEMORY_TYPES), "description": "Category."},
            "content": {"type": "string", "description": "Detailed content of the memory."},
        },
        "required": ["name", "description", "type", "content"],
    },
    handler=_save_memory,
)

register(
    name="list_memories",
    description="List saved cross-session memories, optionally filtered by type.",
    parameters={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(MEMORY_TYPES), "description": "Optional category filter."},
        },
    },
    handler=_list_memories,
)

register(
    name="delete_memory",
    description="Delete a saved cross-session memory by name.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Memory name to delete."},
        },
        "required": ["name"],
    },
    handler=_delete_memory,
)

register(
    name="recall_memory",
    description=(
        "Retrieve memories relevant to a query using code-aware multi-signal scoring. "
        "Use this to look up past context before starting a task. "
        "A recalled memory that names a file or function may be stale — verify before acting on it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query — describe what you're looking for."},
            "top_k": {"type": "integer", "description": "Max results to return (default 5).", "default": 5},
        },
        "required": ["query"],
    },
    handler=_recall_memory,
)
