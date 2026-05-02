"""Context compaction: persist large outputs, micro-compact old results, auto-summarize."""

import json
import re
import time
from pathlib import Path

from nz_coder import config

PREVIEW_CHARS = config.PERSIST_PREVIEW_CHARS
TRIGGER_CHARS = config.PERSIST_OUTPUT_TRIGGER


def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4


def persist_large_output(tool_call_id: str, output: str) -> str:
    if len(output) <= TRIGGER_CHARS:
        return output
    config.TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_call_id or "unknown")
    path = config.TOOL_RESULTS_DIR / f"{safe_id}.txt"
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


def micro_compact(messages: list) -> None:
    """Shorten old tool results to placeholders, keeping recent ones intact."""
    tool_results = []
    for msg in messages:
        if msg.get("role") == "tool":
            tool_results.append(msg)
    if len(tool_results) <= config.KEEP_RECENT_TOOL_RESULTS:
        return
    for msg in tool_results[:-config.KEEP_RECENT_TOOL_RESULTS]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 200:
            msg["content"] = "[Earlier tool result compacted. Re-run the tool if needed.]"


def auto_compact(messages: list, client, model: str, focus: str = None) -> list:
    """Summarize entire conversation and return a fresh continuation message list."""
    config.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    conv_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    prompt = (
        "Summarize this coding-agent conversation for continuity. Structure:\n"
        "1) Task overview and success criteria\n"
        "2) Completed work, files touched\n"
        "3) Key decisions, errors, failed approaches\n"
        "4) Remaining actions and priority\n"
        "5) User preferences and constraints\n"
        "Be concise but preserve critical details.\n"
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
