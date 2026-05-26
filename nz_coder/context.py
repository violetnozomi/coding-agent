"""Context compaction: persist large outputs, micro-compact old results, auto-summarize.

改进点（对标 Claude Code）：
  1. micro_compact 改为按 token 量而非条数截断（大结果优先压缩）
  2. 新增时间间隔触发的 micro_compact（idle 超过阈值则清除老结果）
  3. estimate_tokens 针对 CJK 字符修正权重（减少误差）
"""

import json
import re
import time
from pathlib import Path

from nz_coder import config

PREVIEW_CHARS = config.PERSIST_PREVIEW_CHARS
TRIGGER_CHARS = config.PERSIST_OUTPUT_TRIGGER

# 时间触发 micro_compact 的空闲阈值（分钟），超过则认为 server cache 已凉
# 对标 Claude Code timeBasedMCConfig，保守设为 30 分钟
_TIME_BASED_MC_GAP_MINUTES = 30
# 时间触发时保留最近 N 条 tool result（至少留 1 条，不能全清）
_TIME_BASED_MC_KEEP_RECENT = 3


def estimate_tokens(messages: list) -> int:
    """Estimate token count from messages.

    改进：对 ASCII 以外字符（CJK 等）减少权重，避免 JSON 字节数高估 token 数。
    原方案 len(json) // 4 在 CJK 密集文本下会高估 ~3x，导致过早触发 auto_compact。

    新方案：
    - ASCII 字符：4 bytes per token（JSON 序列化后）
    - 非 ASCII 字符：按 1 token/char 计（CJK 一个字约 1 token）
    """
    serialized = json.dumps(messages, default=str)
    ascii_bytes = sum(1 for c in serialized if ord(c) < 128)
    non_ascii_chars = len(serialized) - ascii_bytes
    # ASCII token 估算：4字节/token；非ASCII：1字节/token（CJK实际约1字/token）
    return ascii_bytes // 4 + non_ascii_chars


def persist_large_output(tool_call_id: str, output: str) -> str:
    if len(output) <= TRIGGER_CHARS:
        return output
    tool_results_dir = config.WORKDIR / ".nz-coder" / "tool-results"
    tool_results_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_call_id or "unknown")
    path = tool_results_dir / f"{safe_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    rel = path.relative_to(config.WORKDIR)
    preview = output[:PREVIEW_CHARS]
    size_kb = len(output) / 1024
    return (
        f"<persisted-output>\n"
        f"Output too large ({size_kb:.1f}KB). Full output saved to: {rel}\n\n"
        f"Preview (first {PREVIEW_CHARS} chars):\n{preview}\n"
        f"</persisted-output>"
    )


def _has_failure_signal(content: str) -> bool:
    """Return True if a tool result contains a traceback or test failure worth keeping."""
    if not isinstance(content, str):
        return False
    return (
        "Traceback (most recent call last)" in content
        or "FAILURES" in content
        or bool(re.search(r"FAILED\s+[\w/\\.\-]+::", content))
        or (content.startswith("Command exited with code") and len(content) > 300)
    )


def _tool_result_size(msg: dict) -> int:
    """Return byte size of a tool result message content."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    return 0


def micro_compact(messages: list) -> None:
    """Shorten tool result content to placeholders.

    改进（对标 Claude Code microCompact）：
    - 按 token 量选择压缩目标，而非简单保留最后 N 条
    - 优先压缩最大的 tool result（收益最高）
    - 保留含 traceback/FAILURES 的结果
    - 新增时间间隔触发：idle 超过阈值则更激进地清除老结果
    """
    # 先尝试时间间隔触发（空闲时 server cache 已凉，清掉不亏）
    if _try_time_based_compact(messages):
        return

    # 常规 micro_compact：总 tool result token 超阈值时，压缩最大的旧结果
    tool_results = [msg for msg in messages if msg.get("role") == "tool"]
    if len(tool_results) <= config.KEEP_RECENT_TOOL_RESULTS:
        return

    # 保护最近 N 条
    protected = set(id(msg) for msg in tool_results[-config.KEEP_RECENT_TOOL_RESULTS:])
    # 按大小降序，优先压缩最大的（Claude Code 策略：最大的压缩收益最高）
    candidates = sorted(
        [msg for msg in tool_results if id(msg) not in protected],
        key=_tool_result_size,
        reverse=True,
    )

    # 计算当前 tool result 总 token 占用，超过阈值才真正压缩
    total_tool_tokens = sum(_tool_result_size(m) for m in tool_results) // 4
    # 如果总量不大，不压缩（避免不必要地丢失信息）
    if total_tool_tokens < 8000:
        return

    for msg in candidates:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 200 and not _has_failure_signal(content):
            msg["content"] = "[Earlier tool result compacted. Re-run the tool if needed.]"


def _try_time_based_compact(messages: list) -> bool:
    """Time-based micro_compact: if agent has been idle > threshold, clear old tool results.

    对标 Claude Code maybeTimeBasedMicrocompact()：
    当空闲超过阈值，server-side prompt cache 已经失效（cache miss 是必然的），
    主动清除老 tool result 内容可以减小下次请求的 context 体积，代价几乎为零。

    Returns True if compaction was performed.
    """
    # 找最近一条 assistant 消息的时间戳
    last_assistant_time: float | None = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            ts = msg.get("_timestamp")
            if ts:
                last_assistant_time = float(ts)
            break

    if last_assistant_time is None:
        return False

    gap_minutes = (time.time() - last_assistant_time) / 60.0
    if gap_minutes < _TIME_BASED_MC_GAP_MINUTES:
        return False

    # idle 超过阈值：清除除最近 _TIME_BASED_MC_KEEP_RECENT 条外的所有 tool result
    tool_results = [msg for msg in messages if msg.get("role") == "tool"]
    if len(tool_results) <= _TIME_BASED_MC_KEEP_RECENT:
        return False

    protected = set(id(msg) for msg in tool_results[-_TIME_BASED_MC_KEEP_RECENT:])
    cleared = 0
    for msg in tool_results:
        if id(msg) not in protected:
            content = msg.get("content", "")
            if isinstance(content, str) and content != "[Earlier tool result compacted. Re-run the tool if needed.]":
                msg["content"] = "[Earlier tool result compacted. Re-run the tool if needed.]"
                cleared += 1

    return cleared > 0


def auto_compact(messages: list, client, model: str, focus: str = None) -> list:
    """Summarize entire conversation and return a fresh continuation message list."""
    transcript_dir = config.WORKDIR / ".nz-coder" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

    _COMPACT_BUDGET = 80000
    selected: list[dict] = []
    budget = _COMPACT_BUDGET
    for msg in reversed(messages):
        serialized = json.dumps(msg, default=str, ensure_ascii=False)
        if len(serialized) > budget:
            break
        selected.append(msg)
        budget -= len(serialized)
    selected.reverse()
    conv_text = json.dumps(selected, default=str, ensure_ascii=False)

    # 改进：summary prompt 包含文件修改状态，防止 compact 后丢失"已编辑"信息
    git_diff_context = _get_git_diff_summary()
    diff_section = f"\n\nCurrent workspace changes (git diff --stat):\n{git_diff_context}" if git_diff_context else ""

    prompt = (
        "Summarize this coding-agent conversation for continuity. Structure:\n"
        "1) Task overview and success criteria\n"
        "2) Completed work, files touched (include exact paths)\n"
        "3) Key decisions, errors, failed approaches\n"
        "4) Remaining actions and priority\n"
        "5) User preferences and constraints\n"
        "Be concise but preserve critical details, especially file paths and error messages.\n"
        + diff_section
    )
    if focus:
        prompt += f"\nPay special attention to: {focus}\n"

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt + "\n\n" + conv_text}],
        max_tokens=4000,
    )
    summary = resp.choices[0].message.content

    return [{
        "role": "user",
        "content": (
            "This session is being continued from a previous conversation that ran out "
            "of context. The summary below covers the earlier portion.\n\n"
            f"{summary}\n\n"
            "Please continue from where we left off without asking further questions."
        ),
    }]


def _get_git_diff_summary() -> str:
    """Get a brief git diff --stat for the current workspace."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--stat", "--no-color"],
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Limit to 20 lines to avoid bloating the summary prompt
            lines = result.stdout.strip().splitlines()
            if len(lines) > 20:
                lines = lines[:20] + [f"... ({len(lines) - 20} more files)"]
            return "\n".join(lines)
    except Exception:
        pass
    return ""
