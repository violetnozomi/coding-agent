"""Working Memory（第一层）：session 内的推理暂存区。

Scratchpad 是 agent 在当前任务中记录假设、尝试、失败原因和发现的地方。
不持久化到磁盘，session 结束清空（AgentLoop.run() 开始时调用 scratchpad.clear()）。

每轮 agent loop 开始时，AgentLoop 会自动把 scratchpad.build_prompt_block()
注入 system prompt 末尾，让 agent 每轮都能看到自己之前的推理记录。
"""
from __future__ import annotations

import time

from nz_coder.tools import register

# ADDED: category 枚举，限定 agent 的记录类型，便于理解记录性质
CATEGORIES = ("hypothesis", "attempt", "failure", "finding", "plan")

# ADDED: 每条 content 上限（字符），截断保护
_MAX_CONTENT_CHARS = 500

# plan 专用上限，避免结构化计划被普通 note 的 500 字符限制截断
_MAX_PLAN_CHARS = 2000

# ADDED: entries 总数上限，超出时淘汰最旧的
_MAX_ENTRIES = 20

# ADDED: build_prompt_block 输出字符上限
_MAX_PROMPT_CHARS = 2000


class Scratchpad:
    """Session-scoped working memory for the agent's reasoning state.

    设计要点：
    - 不持久化，纯内存
    - entries 上限 20 条，超出时淘汰最旧
    - 普通 content 上限 500 字符，plan category 使用更大的专用上限
    - build_prompt_block() 最多返回 2000 字符，并优先保留 plan
    """

    def __init__(self) -> None:
        # ADDED: 每条 entry 格式：{"ts": float, "category": str, "content": str}
        self.entries: list[dict] = []

    def update(self, category: str, content: str) -> str:
        """Agent 主动记录当前推理状态。返回操作结果供工具调用方展示。"""
        if category not in CATEGORIES:
            return f"Error: category must be one of {CATEGORIES}"
        # ADDED: content 截断保护
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "... (truncated)"
        self.entries.append({"ts": time.time(), "category": category, "content": content})
        # ADDED: 超出上限时淘汰最旧
        if len(self.entries) > _MAX_ENTRIES:
            self.entries = self.entries[-_MAX_ENTRIES:]
        preview = content[:80] + ("..." if len(content) > 80 else "")
        return f"Scratchpad updated [{category}]: {preview}"

    def replace_category(self, category: str, content: str, max_chars: int = 0) -> str:
        """替换指定 category 的所有条目为一条新内容。"""
        if category not in CATEGORIES:
            return f"Error: category must be one of {CATEGORIES}"
        if max_chars <= 0:
            max_chars = _MAX_PLAN_CHARS if category == "plan" else _MAX_CONTENT_CHARS
        if len(content) > max_chars:
            content = content[:max_chars] + "... (truncated)"
        self.entries = [e for e in self.entries if e["category"] != category]
        self.entries.append({"ts": time.time(), "category": category, "content": content})
        if len(self.entries) > _MAX_ENTRIES:
            self.entries = self.entries[-_MAX_ENTRIES:]
        preview = content[:80] + ("..." if len(content) > 80 else "")
        return f"Scratchpad [{category}] replaced: {preview}"

    def read(self) -> str:
        """返回所有 scratchpad 内容（时间正序），供 agent 主动查阅。"""
        if not self.entries:
            return "Scratchpad is empty."
        lines = [f"# Scratchpad ({len(self.entries)} entries)", ""]
        for e in self.entries:
            ts_str = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            lines.append(f"[{ts_str}] [{e['category']}] {e['content']}")
        return "\n".join(lines)

    def _format_entry(self, e: dict) -> str:
        ts_str = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        return f"- [{e['category']}] ({ts_str}) {e['content']}"

    def build_prompt_block(self) -> str:
        """生成注入 system prompt 末尾的文本块，plan 优先保留。"""
        if not self.entries:
            return ""
        header = (
            "\n## Working Memory (Scratchpad)\n"
            "Your reasoning notes from this session. "
            "Use these to avoid repeating failed attempts.\n\n"
        )
        total_budget = max(0, _MAX_PROMPT_CHARS - len(header))
        if total_budget <= 0:
            return ""

        plan_entries = [e for e in self.entries if e["category"] == "plan"]
        other_entries = [e for e in self.entries if e["category"] != "plan"]
        plan_budget = min(1200, max(0, total_budget - 200))
        other_budget = total_budget
        lines: list[str] = []

        for e in plan_entries:
            line = self._format_entry(e)
            if len(line) + 1 > plan_budget:
                line = line[:max(0, plan_budget - 17)] + "... (truncated)"
            if line:
                lines.append(line)
                other_budget -= min(len(line) + 1, other_budget)
                break

        other_lines: list[str] = []
        for e in reversed(other_entries):
            line = self._format_entry(e)
            if len(line) + 1 > other_budget:
                break
            other_lines.append(line)
            other_budget -= len(line) + 1
        other_lines.reverse()
        lines.extend(other_lines)

        if not lines:
            return ""
        return header + "\n".join(lines) + "\n"

    def clear(self) -> str:
        """Session 开始或结束时调用，清空所有记录。"""
        count = len(self.entries)
        self.entries = []
        return f"Scratchpad cleared ({count} entries removed)."


# ── 全局实例（由 loop.py 直接引用，不再通过 set_agent_loop 注入）──────────────
# CHANGED: 从依赖注入改为模块级全局实例，工具和 loop.py 共享同一个对象
scratchpad = Scratchpad()


# ── 向后兼容：loop.py 旧版本调用 set_agent_loop，保留为空操作 ─────────────────
def set_agent_loop(loop) -> None:  # noqa: ARG001  — 向后兼容，不再需要
    pass


# ── 工具处理函数 ───────────────────────────────────────────────────────────────

def _update_scratchpad(category: str, content: str) -> str:
    return scratchpad.update(category, content)


def _read_scratchpad() -> str:
    return scratchpad.read()


# ── 工具注册 ───────────────────────────────────────────────────────────────────

# CHANGED: update_scratchpad 新增 category 参数，替代旧版纯字符串替换
register(
    name="update_scratchpad",
    description=(
        "Record your current reasoning state to working memory. "
        "Use this to track what you've tried, what failed, and what you plan next. "
        "The scratchpad is shown automatically at the start of each turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": (
                    "hypothesis: unverified idea; "
                    "attempt: about to try; "
                    "failure: what failed and why; "
                    "finding: confirmed facts; "
                    "plan: next steps"
                ),
            },
            "content": {
                "type": "string",
                "description": "Your note. Keep it concise (max 500 chars).",
            },
        },
        "required": ["category", "content"],
    },
    handler=_update_scratchpad,
)

# CHANGED: read_scratchpad 保持原有接口不变
register(
    name="read_scratchpad",
    description=(
        "Read all entries in your working memory scratchpad. "
        "The scratchpad is also shown automatically each turn; "
        "only call this explicitly if you need the full untruncated history."
    ),
    parameters={"type": "object", "properties": {}},
    handler=_read_scratchpad,
)
