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

import hashlib
import json
import math
import os
import re
import threading
import tempfile
import time
import weakref
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Optional

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import (
    MESSAGE_ID_KEY,
    SYNTHETIC_USER_KEY,
    is_synthetic_user_message,
)
from nz_coder.state.workdir import current_derived_path
from nz_coder.foundation.async_utils import to_thread_settled
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

AUTO_MEMORY_STRUCTURED_TYPES = {"feedback", "project"}
AUTO_MEMORY_INTERNAL_TAGS = (
    "<hook-guidance>",
    "<tool-failure-diagnostic>",
    "<reflection-review>",
    "<session-summary>",
    "<api-error-diagnostic>",
    "<user-frustration-context>",
    "<reminder>",
)
AUTO_MEMORY_DEFAULT_WHY = {
    "feedback": "This came from a verified interaction pattern or repeated failure signal in a prior session.",
    "project": "This captures a durable repository constraint or decision that should survive across tasks.",
}
AUTO_MEMORY_DEFAULT_HOW = {
    "feedback": "Apply this behavior when the same task pattern or failure mode appears again.",
    "project": "Apply this constraint when planning, editing, and verifying changes in this repository.",
}
AUTO_DREAM_STATE_FILE = "auto_dream_state.json"
AUTO_DREAM_REPORT_FILE = "AUTO_DREAM.md"
AUTO_DREAM_MERGE_THRESHOLD = 0.66
AUTO_MEMORY_LOCK = threading.Lock()
AUTO_DREAM_LOCK = threading.Lock()


class _MemoryPipelineCancelled(RuntimeError):
    """Internal cooperative stop that must not advance extraction state."""


def _finite_nonnegative_float(
    value: object,
    *,
    default: float | None = 0.0,
) -> float | None:
    """Normalize one untrusted persisted metric without accepting NaN/Inf."""
    if isinstance(value, bool):
        return default
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(normalized) or normalized < 0:
        return default
    return normalized


def _nonnegative_int(value: object, *, default: int = 0) -> int:
    """Normalize one persisted counter without truncating fractional values."""
    if isinstance(value, bool):
        return default
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or not normalized.is_integer()
    ):
        return default
    return int(normalized)


def _atomic_write_text(path: Path, content: str) -> None:
    """Durably replace one memory artifact without exposing partial text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _synchronized(method):
    """Serialize access to one manager's mutable cache and backend."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class MemoryManager:
    """跨 session 的持久化知识库管理器。"""

    def __init__(
        self,
        memory_dir: Path = None,
        *,
        store=None,
        sync=None,
        backend_name: str = None,
    ) -> None:
        self._lock = threading.RLock()
        self.memory_dir = memory_dir or current_derived_path("MEMORY_DIR")
        self.memories: dict[str, dict] = {}
        self.store = store
        self._sync = sync
        self._backend = backend_name or ("external" if store is not None else "markdown")
        self._migrated = False
        self._snapshot_pulled = 0
        self._sync_bound_txn_ids: set[int] = set()

    @property
    def backend_name(self) -> str:
        return self._backend

    @_synchronized
    def has_memories(self) -> bool:
        if self.memories:
            return True
        if self.store is None:
            return False
        try:
            return bool(self.store.list(limit=1))
        except Exception:
            return False

    @_synchronized
    def attach_sync(self, sync) -> None:
        self._sync = sync
        self._sync_bound_txn_ids.clear()

    @_synchronized
    def get_sync(self):
        return self._sync

    @_synchronized
    def drop_pending_sync(self) -> None:
        if self._sync is not None:
            self._sync.drop()

    @_synchronized
    def ensure_transaction_binding(self, txn) -> None:
        if self._sync is None or self.store is None:
            return
        txn_id = id(txn)
        if txn_id in self._sync_bound_txn_ids:
            return
        self._sync.bind_to_transaction(txn)
        self._sync_bound_txn_ids.add(txn_id)

    @_synchronized
    def backend_status(self) -> dict:
        """Return backend-neutral diagnostics for optional host adapters."""
        return {
            "backend": self._backend,
            "snapshot_pulled": self._snapshot_pulled,
            "sync_enabled": bool(self._sync is not None and getattr(self._sync, "enabled", False)),
            "vec": bool(self.store is not None and getattr(self.store, "vec_enabled", False)),
        }

    @_synchronized
    def load_all(self) -> None:
        """Load memories from the injected store or native Markdown backend."""
        if self.store is not None:
            self._load_store_memories()
            return
        self._load_markdown_memories()

    def _load_markdown_memories(self) -> None:
        self.memories = {}
        if not self.memory_dir.exists():
            return
        for md in sorted(self.memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            try:
                parsed = self._parse_frontmatter(md.read_text(encoding="utf-8"))
                if not parsed:
                    continue
                name = parsed.get("name", md.stem)
                created_at = _finite_nonnegative_float(
                    parsed.get("created_at", 0.0),
                    default=None,
                )
                last_accessed = _finite_nonnegative_float(
                    parsed.get("last_accessed", 0.0),
                    default=None,
                )
                if created_at is None or last_accessed is None:
                    raise ValueError("memory timestamps must be finite")
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "created_at": created_at,
                    "last_accessed": last_accessed,
                    "access_count": _nonnegative_int(
                        parsed.get("access_count", 0)
                    ),
                    "file": md.name,
                }
            except (OSError, TypeError, ValueError):
                continue

    def _load_store_memories(self) -> None:
        self.store.connect()
        if self._sync is not None and self._sync.enabled and not getattr(self._sync, "last_pull_ok", False):
            self._snapshot_pulled = self._sync.pull_snapshot(reset_local=True)
        if not self._migrated:
            self._migrated = True
            should_migrate = (
                not self.store.list(limit=1)
                and self.memory_dir.exists()
                and not (self._sync is not None and self._sync.enabled and getattr(self._sync, "last_pull_ok", False))
            )
            if should_migrate:
                self._migrate_legacy_md_to_store()
        self._refresh_memories_cache()
        if self.memories:
            self._rebuild_index()

    def _migrate_legacy_md_to_store(self) -> None:
        for md in sorted(self.memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            try:
                parsed = self._parse_frontmatter(md.read_text(encoding="utf-8"))
            except OSError:
                continue
            if not parsed:
                continue
            name = parsed.get("name", md.stem)
            created_at = _finite_nonnegative_float(
                parsed.get("created_at", 0.0),
                default=None,
            )
            last_accessed = _finite_nonnegative_float(
                parsed.get("last_accessed", 0.0),
                default=None,
            )
            if created_at is None or last_accessed is None:
                continue
            self.store.upsert({
                "id": self._safe_name(name),
                "name": name,
                "description": parsed.get("description", ""),
                "type": parsed.get("type", "project"),
                "content": parsed.get("content", ""),
                "created": created_at or None,
                "updated": last_accessed or created_at or None,
                "last_accessed": last_accessed or created_at or None,
                "access_count": _nonnegative_int(parsed.get("access_count", 0)),
            })

    def _to_public(self, rec: dict) -> dict:
        name = rec.get("name") or rec.get("id")
        return {
            "name": name,
            "description": rec.get("description", "") or "",
            "type": rec.get("type", "project") or "project",
            "content": rec.get("content", "") or "",
            "created_at": _finite_nonnegative_float(rec.get("created")) or 0.0,
            "last_accessed": (
                _finite_nonnegative_float(rec.get("last_accessed")) or 0.0
            ),
            "access_count": _nonnegative_int(rec.get("access_count")),
            "file": f"{self._safe_name(name)}.md",
        }

    def _refresh_memories_cache(self) -> None:
        self.memories = {}
        if self.store is None:
            return
        for rec in self.store.list(limit=10000):
            pub = self._to_public(rec)
            self.memories[pub["name"]] = {
                "description": pub["description"],
                "type": pub["type"],
                "content": pub["content"],
                "created_at": pub["created_at"],
                "last_accessed": pub["last_accessed"],
                "access_count": pub["access_count"],
                "file": pub["file"],
            }

    @_synchronized
    def scan_headers(self) -> list[dict]:
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

    @_synchronized
    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        if self.store is not None:
            if not (query or "").strip():
                return []
            self.store.connect()
            results = [self._to_public(r) for r in self.store.search(query, top_k=top_k)]
            touched_at = time.time()
            for item in results:
                cached = self.memories.get(item["name"])
                if cached:
                    cached["last_accessed"] = touched_at
                    cached["access_count"] = cached.get("access_count", 0) + 1
                else:
                    self.memories[item["name"]] = {
                        "description": item["description"],
                        "type": item["type"],
                        "content": item["content"],
                        "created_at": item["created_at"],
                        "last_accessed": touched_at,
                        "access_count": item["access_count"],
                        "file": item["file"],
                    }
            return results

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

    @_synchronized
    def build_prompt_block(
        self,
        query: Optional[str] = None,
        max_items: int = 8,
        max_chars: int = 3000,
        rerank_client=None,
        model: str | None = None,
        rerank_provider=None,
        rerank_capabilities=None,
        rerank_observer=None,
    ) -> str:
        if not self.has_memories():
            return ""

        max_items = max(1, int(max_items or 8))
        user_items = self._user_preference_items(max_items)
        remaining_slots = max(0, max_items - len(user_items))

        if query and remaining_slots:
            candidates = self.recall(query, top_k=max(remaining_slots * 3, remaining_slots))
            candidates = [item for item in candidates if item.get("type") != "user"]
            candidates = rerank_memories(
                query,
                candidates,
                rerank_client,
                model,
                remaining_slots,
                provider=rerank_provider,
                capabilities=rerank_capabilities,
                observer=rerank_observer,
            )
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

    @_synchronized
    def save(self, name: str, description: str, mem_type: str, content: str) -> str:
        if "\n" in str(name) or "\r" in str(name):
            return "Error: memory name must be a single line"
        if "\n" in str(description) or "\r" in str(description):
            return "Error: memory description must be a single line"
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        safe_name = self._safe_name(name)
        if not safe_name:
            return "Error: invalid memory name"

        if self.store is not None:
            return self._save_store_memory(name, description, mem_type, content, safe_name)

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

    def _save_store_memory(self, name: str, description: str, mem_type: str, content: str, safe_name: str) -> str:
        self.store.connect()
        now = time.time()
        existing_entry = self.memories.get(name)
        existing_store = self.store.get(safe_name)

        if existing_entry is None and existing_store is None:
            merge_target = self._find_merge_target(name, description, mem_type, content)
            if merge_target:
                target_name, target_mem = merge_target
                merged = self._merge_memory(target_name, target_mem, description, content, now)
                saved = self.store.upsert({
                    "id": self._safe_name(target_name),
                    "name": target_name,
                    "description": merged["description"],
                    "type": merged["type"],
                    "content": merged["content"],
                    "created": merged["created_at"],
                    "updated": now,
                    "last_accessed": now,
                    "access_count": target_mem["access_count"],
                })
                if self._sync is not None:
                    self._sync.enqueue(saved)
                self._refresh_memories_cache()
                self._rebuild_index()
                return f"Merged memory '{name}' into existing memory '{target_name}' [{mem_type}]"

        created_at = float(existing_store["created"]) if existing_store else (existing_entry["created_at"] if existing_entry else now)
        access_count = int(existing_store["access_count"]) if existing_store else (existing_entry["access_count"] if existing_entry else 0)
        saved = self.store.upsert({
            "id": safe_name,
            "name": name,
            "description": description,
            "type": mem_type,
            "content": content,
            "created": created_at,
            "updated": now,
            "last_accessed": now,
            "access_count": access_count,
        })
        if self._sync is not None:
            self._sync.enqueue(saved)
        self._refresh_memories_cache()
        self._rebuild_index()
        action = "Updated" if (existing_store is not None or existing_entry is not None) else "Saved"
        return f"{action} memory '{name}' [{mem_type}]"

    def _write_memory_file(self, name: str, mem: dict) -> None:
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
        _atomic_write_text(self.memory_dir / fname, frontmatter)

    def _find_merge_target(
        self,
        name: str,
        description: str,
        mem_type: str,
        content: str,
    ) -> tuple[str, dict] | None:
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

    @_synchronized
    def list_memories(self, mem_type: str = None) -> str:
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

    @_synchronized
    def delete(self, name: str) -> str:
        safe_name = self._safe_name(name)
        if not safe_name:
            return "Error: invalid memory name"

        if self.store is not None:
            self.store.connect()
            removed = []
            for existing in list(self.memories):
                if existing == name or self._safe_name(existing) == safe_name:
                    removed.append(existing)
            existed = self.store.get(safe_name) is not None
            if not existed and not removed:
                return f"Error: memory not found: {name}"
            if existed:
                self.store.delete(safe_name)
                if self._sync is not None:
                    self._sync.enqueue_delete(safe_name)
            self._refresh_memories_cache()
            self._rebuild_index()
            label = ", ".join(removed) if removed else name
            return f"Deleted memory: {label}"

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

    @_synchronized
    def cleanup(self, days_threshold: int = _CLEANUP_DAYS_DEFAULT) -> str:
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

    @_synchronized
    def auto_dream(self, days_threshold: int = _CLEANUP_DAYS_DEFAULT) -> dict:
        now = time.time()
        merged: list[dict] = []
        deleted: list[str] = []
        staled: list[str] = []

        sorted_names = sorted(
            list(self.memories),
            key=lambda name: (
                self.memories[name].get("type", "project"),
                float(self.memories[name].get("created_at", 0.0) or 0.0),
                name,
            ),
        )
        for index, anchor_name in enumerate(sorted_names):
            anchor = self.memories.get(anchor_name)
            if anchor is None:
                continue
            for other_name in sorted_names[index + 1:]:
                anchor = self.memories.get(anchor_name)
                other = self.memories.get(other_name)
                if anchor is None or other is None:
                    continue
                if anchor.get("type") != other.get("type"):
                    continue
                same_text = (
                    _normalized_memory_text(anchor["description"], anchor["content"])
                    == _normalized_memory_text(other["description"], other["content"])
                )
                similar = _memory_similarity(
                    _memory_text(anchor_name, anchor),
                    _memory_text(other_name, other),
                    min_tokens=_SIMILARITY_MIN_TOKENS_FOR_MERGE,
                ) >= AUTO_DREAM_MERGE_THRESHOLD
                if not (same_text or similar):
                    continue
                merged_entry = self._merge_memory(anchor_name, anchor, other["description"], other["content"], now)
                self.save(anchor_name, merged_entry["description"], anchor["type"], merged_entry["content"])
                self.delete(other_name)
                merged.append({"into": anchor_name, "from": other_name, "type": anchor["type"]})

        threshold_secs = max(1, int(days_threshold or _CLEANUP_DAYS_DEFAULT)) * 86400
        for name, mem in list(self.memories.items()):
            age_secs = now - float(mem.get("created_at", 0.0) or 0.0)
            last_accessed = float(mem.get("last_accessed", 0.0) or 0.0)
            idle_secs = now - last_accessed if last_accessed else age_secs
            access_count = int(mem.get("access_count", 0) or 0)
            if access_count == 0 and age_secs > threshold_secs:
                result = self.delete(name)
                if not result.startswith("Error:"):
                    deleted.append(name)
            elif access_count > 0 and idle_secs > threshold_secs and not mem["description"].endswith("[stale]"):
                result = self.save(name, mem["description"] + " [stale]", mem["type"], mem["content"])
                if not result.startswith("Error:"):
                    staled.append(name)

        report = {
            "ran_at": now,
            "merged": merged,
            "deleted": deleted,
            "staled": staled,
            "memory_count": len(self.memories),
        }
        self._write_auto_dream_report(report)
        return report


    def _touch(self, name: str) -> None:
        if name not in self.memories:
            return
        mem = self.memories[name]
        mem["last_accessed"] = time.time()
        mem["access_count"] = mem["access_count"] + 1
        if self.store is not None:
            self.store.touch(self._safe_name(name))
            self._rebuild_index()
            return

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
            _atomic_write_text(fp, frontmatter)

    def _rebuild_index(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
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
        all_lines = content.splitlines()
        if len(all_lines) > MAX_INDEX_LINES:
            truncated = "\n".join(all_lines[:MAX_INDEX_LINES])
            over = len(all_lines) - MAX_INDEX_LINES
            truncated += (
                f"\n\n> WARNING: MEMORY.md has {len(all_lines)} lines (limit: {MAX_INDEX_LINES}). "
                f"{over} entries omitted. Use `recall_memory` to search all memories."
            )
            content = truncated + "\n"

        if len(content.encode("utf-8")) > MAX_INDEX_BYTES:
            cut = content.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")
            last_nl = cut.rfind("\n")
            cut = cut[:last_nl] if last_nl > 0 else cut
            content = (
                cut
                + "\n\n> WARNING: MEMORY.md exceeds byte limit. Some entries were cut. Keep descriptions concise.\n"
            )

        index_path = self.memory_dir / "MEMORY.md"
        _atomic_write_text(index_path, content)

    def _write_auto_dream_report(self, report: dict) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Auto Dream Report",
            "",
            f"- ran_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.get('ran_at', time.time())))}",
            f"- merged: {len(report.get('merged', []))}",
            f"- deleted: {len(report.get('deleted', []))}",
            f"- staled: {len(report.get('staled', []))}",
            f"- memory_count: {report.get('memory_count', len(self.memories))}",
            "",
        ]
        merged = report.get("merged", [])
        if merged:
            lines.append("## Merged")
            for item in merged:
                lines.append(f"- [{item.get('type', 'project')}] {item.get('from')} -> {item.get('into')}")
            lines.append("")
        deleted = report.get("deleted", [])
        if deleted:
            lines.append("## Deleted")
            for name in deleted:
                lines.append(f"- {name}")
            lines.append("")
        staled = report.get("staled", [])
        if staled:
            lines.append("## Marked Stale")
            for name in staled:
                lines.append(f"- {name}")
            lines.append("")
        if not merged and not deleted and not staled:
            lines.extend(["## Summary", "- No changes were needed.", ""])
        _atomic_write_text(
            self.memory_dir / AUTO_DREAM_REPORT_FILE,
            "\n".join(lines),
        )


    def _parse_frontmatter(self, text: str) -> Optional[dict]:
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
    *,
    provider=None,
    capabilities=None,
    observer=None,
    cancel_event: threading.Event | None = None,
) -> list[dict]:
    """从对话历史中提取值得跨 session 保存的经验。

    默认使用规则提取，传入 client/model 时追加一次 LLM 结构化事实提取。
    LLM 调用失败时返回规则提取结果，不影响 agent 主流程。
    """
    candidates = _extract_rule_based_learnings(messages)
    if client is not None and model:
        candidates.extend(_extract_llm_session_learnings(
            messages,
            client,
            model,
            provider=provider,
            capabilities=capabilities,
            observer=observer,
            cancel_event=cancel_event,
        ))
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
        if msg.get("role") != "user" or is_synthetic_user_message(msg):
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
                        "type": _classify_explicit_memory_type(remainder),
                        "content": remainder,
                        "confidence": 0.99,
                        "reason": "explicit user memory request",
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


def _extract_llm_session_learnings(
    messages: list[dict],
    client,
    model: str,
    *,
    provider=None,
    capabilities=None,
    observer=None,
    cancel_event: threading.Event | None = None,
) -> list[dict]:
    """用 LLM 从最近对话中提取结构化长期记忆候选。"""
    excerpt = _conversation_excerpt(messages)
    if not excerpt:
        return []
    prompt = (
        "Extract durable layer-2 memories for a coding agent from the conversation delta below. "
        "Return only a JSON object with key `memories`, whose value is a list of objects with fields "
        "name, description, type, content. Type must be one of: "
        f"{', '.join(MEMORY_TYPES)}. "
        "Only keep durable items in these categories: user identity/preferences, verified feedback patterns, "
        "project decisions/constraints, and reusable external references. "
        "Do NOT save codebase-derivable architecture info, file/class/function names, current implementation details, "
        "Git-history facts, information that tools can fetch on demand, or temporary one-off task state. "
        "For feedback and project memories, content must use this markdown structure exactly: "
        "`Rule: ...`, `**Why:** ...`, `**How to apply:** ...`. "
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
            provider=provider,
            capabilities=capabilities,
            observer=observer,
            cancel_event=cancel_event,
        )
        raw = resp
        data = _parse_json_list(raw)
    except _MemoryPipelineCancelled:
        raise
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
        normalized = _canonicalize_learning_candidate(item)
        if normalized is None:
            continue
        text = f"{normalized.get('description', '')} {normalized.get('content', '')}"
        if any(
            _memory_similarity(text, f"{old['description']} {old['content']}") >= _SIMILARITY_MERGE_THRESHOLD
            for old in deduped
        ):
            continue
        deduped.append(normalized)
    return deduped



# ── Layer 2 / Layer 3 helpers ─────────────────────────────────────────────────


def _read_json_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
    )


def _filter_auto_memory_messages(messages: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if is_synthetic_user_message(msg):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content", "")
        if role not in {"user", "assistant", "tool"} or not isinstance(content, str):
            continue
        stripped = content.strip()
        if not stripped:
            continue
        if any(stripped.startswith(tag) for tag in AUTO_MEMORY_INTERNAL_TAGS):
            continue
        if role == "tool" and "relevant memory(ies)" in stripped:
            continue
        filtered.append({"role": role, "content": stripped})
    return filtered


def _classify_explicit_memory_type(text: str) -> str:
    lowered = str(text or "").lower()
    if re.search(r"https?://", lowered) or any(token in lowered for token in ("dashboard", "grafana", "sentry", "api ", "api地址", "文档", "链接", "地址")):
        return "reference"
    if any(token in lowered for token in ("prefer", "please use", "reply in", "style", "tone", "tabs", "spaces", "中文", "英文", "简洁", "详细", "喜欢", "偏好", "请用", "回复")):
        return "user"
    if any(token in lowered for token in ("this project", "project uses", "repository", "repo", "constraint", "decision", "must use", "avoid", "项目", "仓库", "约束", "决定", "必须", "不要")):
        return "project"
    return "feedback"


def _coerce_structured_memory_content(description: str, content: str, mem_type: str) -> str:
    stripped = str(content or "").strip()
    if all(marker in stripped for marker in ("Rule:", "**Why:**", "**How to apply:**")):
        return stripped[:1600]
    rule = _clean_one_line(description or stripped.splitlines()[0] if stripped else description)[:180]
    why = _clean_one_line(stripped)[:500] if stripped else AUTO_MEMORY_DEFAULT_WHY[mem_type]
    if not why or why.lower() == rule.lower():
        why = AUTO_MEMORY_DEFAULT_WHY[mem_type]
    how = AUTO_MEMORY_DEFAULT_HOW[mem_type]
    return f"Rule: {rule}\n\n**Why:** {why}\n\n**How to apply:** {how}"


def _canonicalize_learning_candidate(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    mem_type = str(item.get("type", "project") or "project").strip().lower()
    if mem_type not in MEMORY_TYPES:
        mem_type = "project"
    name = _candidate_name(str(item.get("name", "memory")))
    description = _clean_one_line(str(item.get("description", "")))[:160]
    content = str(item.get("content", "")).strip()[:1600]
    if not name or not description or not content:
        return None
    if mem_type in AUTO_MEMORY_STRUCTURED_TYPES:
        content = _coerce_structured_memory_content(description, content, mem_type)
    return {
        "name": name,
        "description": description,
        "type": mem_type,
        "content": content,
        "confidence": min(
            1.0,
            _finite_nonnegative_float(
                item.get("confidence", 0.75),
                default=0.0,
            ) or 0.0,
        ),
        "reason": _clean_one_line(str(item.get("reason", "automatic extraction")))[:300],
    }


def _current_session_ids(session_id: str | None = None) -> list[str]:
    from nz_coder.state.sessions import list_session_ids

    ids = set(list_session_ids())
    if session_id:
        ids.add(str(session_id))
    return sorted(ids)


def maybe_run_auto_dream(session_id: str | None = None, *, tracer=None) -> dict:
    if not getattr(config, "MEMORY_AUTO_DREAM", True):
        return {"status": "disabled", "reason": "feature_flag"}
    if not AUTO_DREAM_LOCK.acquire(blocking=False):
        return {"status": "busy", "reason": "lock_held"}
    try:
        now = time.time()
        state_path = current_memory_manager().memory_dir / AUTO_DREAM_STATE_FILE
        state = _read_json_file(state_path)
        current_ids = _current_session_ids(session_id)
        previous_ids = set(state.get("session_ids_at_last_run", []) or [])
        new_ids = sorted(set(current_ids) - previous_ids)
        min_hours = max(1, int(getattr(config, "MEMORY_AUTO_DREAM_MIN_HOURS", 24) or 24))
        min_sessions = max(1, int(getattr(config, "MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS", 5) or 5))
        last_run_at = _finite_nonnegative_float(
            state.get("last_run_at"),
        ) or 0.0
        if last_run_at and now - last_run_at < min_hours * 3600:
            return {
                "status": "skipped",
                "reason": "cooldown",
                "new_session_count": len(new_ids),
                "hours_since_last_run": (now - last_run_at) / 3600.0,
            }
        if len(new_ids) < min_sessions:
            return {
                "status": "skipped",
                "reason": "session_threshold",
                "new_session_count": len(new_ids),
            }
        report = current_memory_manager().auto_dream(
            days_threshold=getattr(config, "MEMORY_CLEANUP_DAYS", _CLEANUP_DAYS_DEFAULT),
        )
        summary = {
            "status": "ran",
            "ran_at": report.get("ran_at", now),
            "merged_count": len(report.get("merged", [])),
            "deleted_count": len(report.get("deleted", [])),
            "staled_count": len(report.get("staled", [])),
            "new_session_count": len(new_ids),
            "session_ids_at_last_run": current_ids,
        }
        _write_json_file(
            state_path,
            {
                "last_run_at": float(summary["ran_at"]),
                "session_ids_at_last_run": current_ids,
                "last_report": summary,
            },
        )
        if tracer is not None:
            tracer.log("auto_dream", **summary)
        return summary
    finally:
        AUTO_DREAM_LOCK.release()


def _memory_message_snapshots(messages: list[dict]) -> list[dict]:
    """Keep identity and synthetic provenance across the extraction boundary."""
    return [
        {
            "role": msg.get("role"),
            "content": msg.get("content", ""),
            MESSAGE_ID_KEY: msg.get(MESSAGE_ID_KEY),
            SYNTHETIC_USER_KEY: bool(msg.get(SYNTHETIC_USER_KEY, False)),
        }
        for msg in messages
        if isinstance(msg, dict)
    ]


def _memory_message_keys(messages: list[dict]) -> list[str]:
    """Build compaction-stable extraction cursors, preferring message IDs."""
    occurrences: dict[str, int] = {}
    keys: list[str] = []
    for msg in messages:
        message_id = msg.get(MESSAGE_ID_KEY)
        if isinstance(message_id, str) and message_id:
            keys.append(f"id:{message_id}")
            continue
        canonical = json.dumps(
            {
                "role": msg.get("role"),
                "content": msg.get("content", ""),
                "synthetic": bool(msg.get(SYNTHETIC_USER_KEY, False)),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        occurrence = occurrences.get(digest, 0)
        occurrences[digest] = occurrence + 1
        keys.append(f"hash:{digest}:{occurrence}")
    return keys


def _merge_processed_message_keys(existing: list[str], current: list[str]) -> list[str]:
    """Keep a bounded ordered set of processed identities in session state."""
    merged: list[str] = []
    seen: set[str] = set()
    for key in [*existing, *current]:
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        merged.append(key)
    return merged[-512:]


def run_auto_memory_pipeline(
    session_id: str,
    messages: list[dict],
    *,
    client=None,
    model: str | None = None,
    tracer=None,
    provider=None,
    capabilities=None,
    observer=None,
    cancel_event: threading.Event | None = None,
) -> dict:
    if not getattr(config, "MEMORY_AUTO_EXTRACT", True):
        return {"status": "disabled", "reason": "feature_flag"}
    if not AUTO_MEMORY_LOCK.acquire(blocking=False):
        return {"status": "busy", "reason": "lock_held"}
    try:
        from nz_coder.state.sessions import session_memory_state_path

        state_path = session_memory_state_path(session_id)
        state = _read_json_file(state_path)
        snapshots = _memory_message_snapshots(messages)
        current_keys = _memory_message_keys(snapshots)
        processed_keys = [
            key for key in state.get("processed_message_keys", [])
            if isinstance(key, str)
        ]
        if processed_keys:
            processed = set(processed_keys)
            window = [
                message for message, key in zip(snapshots, current_keys)
                if key not in processed
            ]
        else:
            # Backward-compatible migration from the old count-only cursor.
            last_count = min(
                _nonnegative_int(state.get("last_message_count")),
                len(snapshots),
            )
            window = snapshots[last_count:]
        filtered = _filter_auto_memory_messages(window)
        saved_names: list[str] = []
        pending_review_count = 0
        proposal_ids: list[str] = []
        candidates: list[dict] = []
        if filtered:
            from nz_coder.state.memory_control import MemoryControlPlane

            candidates = extract_session_learnings(
                filtered,
                client=client,
                model=model,
                provider=provider,
                capabilities=capabilities,
                observer=observer,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise _MemoryPipelineCancelled("memory extraction cancelled")
            manager = current_memory_manager()
            control = MemoryControlPlane(manager.memory_dir, manager)
            source_message_ids = tuple(
                str(message.get(MESSAGE_ID_KEY))
                for message in window
                if isinstance(message.get(MESSAGE_ID_KEY), str)
                and message.get(MESSAGE_ID_KEY)
            )
            for candidate in candidates:
                if cancel_event is not None and cancel_event.is_set():
                    raise _MemoryPipelineCancelled("memory extraction cancelled")
                proposal = control.submit(
                    candidate,
                    source_session=str(session_id or ""),
                    source_message_ids=source_message_ids,
                )
                proposal_ids.append(proposal.fingerprint)
                result = proposal.status
                if tracer is not None:
                    tracer.log(
                        "auto_memory_proposal",
                        name=candidate["name"],
                        mem_type=candidate["type"],
                        result=result,
                        fingerprint=proposal.fingerprint,
                        risk=proposal.risk,
                    )
                if proposal.status == "applied":
                    saved_names.append(candidate["name"])
                elif proposal.status == "pending_review":
                    pending_review_count += 1

        if cancel_event is not None and cancel_event.is_set():
            raise _MemoryPipelineCancelled("memory extraction cancelled")
        now = time.time()
        next_state = {
            "session_id": str(session_id or ""),
            "last_message_count": len(messages),
            "processed_message_keys": _merge_processed_message_keys(
                processed_keys,
                current_keys,
            ),
            "last_extracted_at": now,
            "total_extractions": _nonnegative_int(
                state.get("total_extractions")
            ) + 1,
            "total_saved": _nonnegative_int(
                state.get("total_saved")
            ) + len(saved_names),
            "last_saved_names": saved_names[-10:],
        }
        _write_json_file(state_path, next_state)

        dream = maybe_run_auto_dream(session_id, tracer=tracer)
        summary = {
            "status": "ok",
            "window_message_count": len(window),
            "filtered_message_count": len(filtered),
            "candidate_count": len(candidates),
            "saved_count": len(saved_names),
            "saved_names": saved_names,
            "pending_review_count": pending_review_count,
            "proposal_ids": proposal_ids,
            "state_path": str(state_path),
            "dream": dream,
        }
        if tracer is not None:
            tracer.log("auto_memory_pipeline", **{k: v for k, v in summary.items() if k != "dream"})
        return summary
    finally:
        AUTO_MEMORY_LOCK.release()

async def run_auto_memory_pipeline_async(
    session_id: str,
    messages: list[dict],
    *,
    client=None,
    model: str | None = None,
    tracer=None,
    provider=None,
    capabilities=None,
    observer=None,
) -> dict:
    """Async wrapper for the auto-memory extraction pipeline."""
    cancel_event = threading.Event()
    return await to_thread_settled(
        run_auto_memory_pipeline,
        session_id,
        messages,
        client=client,
        model=model,
        tracer=tracer,
        provider=provider,
        capabilities=capabilities,
        observer=observer,
        cancel_event=cancel_event,
        cancel_callback=cancel_event.set,
    )


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


def _create_chat_completion(
    client,
    *,
    provider=None,
    capabilities=None,
    observer=None,
    cancel_event=None,
    **kwargs,
):
    """优先使用 JSON mode；不支持时回退到普通 completion。"""
    from nz_coder.model_gateway import (
        ModelCall,
        ModelCallPurpose,
        ModelCallStatus,
        ModelSelectionRequest,
        OpenAIClientBridgeProvider,
        ProductionModelGateway,
        resolve_model_runtime,
    )

    active_provider = provider or OpenAIClientBridgeProvider()
    runtime = resolve_model_runtime(ModelSelectionRequest(
        provider_name=str(
            getattr(active_provider, "name", "openai-compatible")
        ),
        model_id=str(kwargs["model"]),
        provider=active_provider,
        client=client,
        owns_client=False,
    ))
    if capabilities is not None:
        runtime.capabilities = capabilities
    outcome = ProductionModelGateway(runtime, observer=observer).complete_sync(
        ModelCall(
            purpose=ModelCallPurpose.MEMORY,
            messages=kwargs["messages"],
            max_output_tokens=int(kwargs.get("max_tokens") or 800),
            response_format=kwargs.get("response_format"),
            metadata={"allow_response_format_fallback": True},
            timeout_seconds=600.0,
        ),
        cancel_event=cancel_event,
    )
    if outcome.status is ModelCallStatus.CANCELLED:
        raise _MemoryPipelineCancelled(outcome.error or "memory extraction cancelled")
    if outcome.status is not ModelCallStatus.COMPLETED:
        raise RuntimeError(outcome.error or outcome.status.value)
    return outcome.content


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
    *,
    provider=None,
    capabilities=None,
    observer=None,
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
        raw = _create_chat_completion(
            client,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            provider=provider,
            capabilities=capabilities,
            observer=observer,
        )
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
_MEMORY_MANAGER: ContextVar[MemoryManager | None] = ContextVar(
    "nz_coder_memory_manager",
    default=None,
)
_WORKSPACE_MEMORY_MANAGERS: weakref.WeakValueDictionary[str, MemoryManager] = (
    weakref.WeakValueDictionary()
)
_WORKSPACE_MEMORY_MANAGERS_LOCK = threading.Lock()
_WORKSPACE_MEMORY_MANAGERS[str(memory_mgr.memory_dir.resolve())] = memory_mgr


def current_memory_manager() -> MemoryManager:
    """Return the memory manager bound to the current agent context."""
    return _MEMORY_MANAGER.get() or memory_mgr


def workspace_memory_manager(memory_dir: Path | None = None) -> MemoryManager:
    """Return the shared Markdown manager for one resolved workspace path.

    Long-term memory is workspace-scoped rather than Session-scoped. Sharing
    its manager gives concurrent HTTP/CLI sessions one cache and one mutation
    lock while the path key prevents state from crossing workspaces. An
    explicitly bound adapter remains authoritative for dependency injection.
    """
    selected = (memory_dir or current_derived_path("MEMORY_DIR")).resolve()
    bound = _MEMORY_MANAGER.get()
    if bound is not None and bound.memory_dir.resolve() == selected:
        return bound
    key = str(selected)
    with _WORKSPACE_MEMORY_MANAGERS_LOCK:
        existing = _WORKSPACE_MEMORY_MANAGERS.get(key)
        if existing is not None:
            return existing
        manager = MemoryManager(selected)
        _WORKSPACE_MEMORY_MANAGERS[key] = manager
        return manager


@contextmanager
def bind_memory_manager(manager: MemoryManager):
    """Temporarily bind a memory manager to the current execution context."""
    token = _MEMORY_MANAGER.set(manager)
    try:
        yield manager
    finally:
        _MEMORY_MANAGER.reset(token)


# ── 工具处理函数 ──────────────────────────────────────────────────────────────

def _save_memory(name: str, description: str, type: str, content: str) -> str:
    return current_memory_manager().save(name, description, type, content)


def _list_memories(type: str = None) -> str:
    return current_memory_manager().list_memories(type)


def _delete_memory(name: str) -> str:
    return current_memory_manager().delete(name)


def _recall_memory(query: str, top_k: int = 5) -> str:
    results = current_memory_manager().recall(query, top_k)
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
    side_effect="readonly",
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
    side_effect="readonly",
)
