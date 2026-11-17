"""压缩摘要的唯一输出结构、已知字段投影与固定栏目渲染。"""
from __future__ import annotations

import json

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError


SUMMARY_INSTRUCTIONS = """Return exactly one JSON object, without Markdown fences or surrounding prose:
{
  "goal": "Current task and success criteria",
  "constraints": ["User constraints, preferences and project rules"],
  "progress": {"done": [], "in_progress": [], "blocked": []},
  "findings": [{"summary": "Important finding", "certainty": "confirmed", "basis": "Evidence or uncertainty"}],
  "decisions": [{"decision": "Choice made", "reason": "Why"}],
  "verification": [{"command": "Exact command", "result": "Observed result and whether it is still current"}],
  "next_steps": ["Ordered next action"],
  "relevant_files": [{"path": "Exact path", "reason": "Purpose and modification status"}]
}
Keep every top-level field and all three progress fields. Arrays may be empty.
All leaf values must be strings. certainty is only "confirmed" or "hypothesis".
goal, list items, summary, decision, result and path must not be empty.
basis, reason and command may be empty when unavailable; never invent missing evidence.
Preserve costly confirmed discoveries, decisions and reasons, exact commands, important errors,
file paths, current edits, unresolved hypotheses, existing artifact IDs and the next concrete action.
Distinguish observed evidence from inference, and historical tests from current verification.
Do not add fields or promote uncertain claims merely to populate this structure.
"""

_FIELDS = (
    "goal", "constraints", "progress", "findings", "decisions", "verification",
    "next_steps", "relevant_files",
)


def _invalid(path: str, reason: str) -> PublicRuntimeError:
    return PublicRuntimeError(PublicError(
        "invalid_compaction_summary", f"Invalid compaction summary at {path}: {reason}",
    ))


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _reject_constant(_value: str):
    raise ValueError("non-standard JSON number")


def _text(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _invalid(path, "expected a string" if allow_empty else "expected a nonempty string")
    return value


def _strings(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise _invalid(path, "expected an array")
    return [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]


def parse_compaction_summary(raw: str) -> tuple[dict, int]:
    """只发布校验后的已知字段；未知整块不进入后续上下文。"""
    if not isinstance(raw, str):
        raise _invalid("JSON", "expected one JSON object")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ValueError:
        raise _invalid("JSON", "expected one standard JSON object without duplicate fields") from None
    discarded = 0

    def fields(value: object, names: tuple[str, ...], path: str) -> dict:
        nonlocal discarded
        if not isinstance(value, dict):
            raise _invalid(path, "expected an object")
        for name in names:
            if name not in value:
                raise _invalid(f"{path}.{name}", "required field is missing")
        discarded += len(value.keys() - set(names))
        return {name: value[name] for name in names}

    def records(value: object, names: tuple[str, ...], path: str, *, empty: tuple[str, ...] = ()) -> list[dict]:
        if not isinstance(value, list):
            raise _invalid(path, "expected an array")
        result = []
        for index, item in enumerate(value):
            entry_path = f"{path}[{index}]"
            entry = fields(item, names, entry_path)
            result.append({name: _text(entry[name], f"{entry_path}.{name}", allow_empty=name in empty)
                           for name in names})
        return result

    value = fields(document, _FIELDS, "summary")
    progress = fields(value["progress"], ("done", "in_progress", "blocked"), "progress")
    findings = records(value["findings"], ("summary", "certainty", "basis"), "findings", empty=("basis",))
    for index, finding in enumerate(findings):
        if finding["certainty"] not in ("confirmed", "hypothesis"):
            raise _invalid(f"findings[{index}].certainty", "expected confirmed or hypothesis")
    result = {
        "goal": _text(value["goal"], "goal"),
        "constraints": _strings(value["constraints"], "constraints"),
        "progress": {name: _strings(items, f"progress.{name}") for name, items in progress.items()},
        "findings": findings,
        "decisions": records(value["decisions"], ("decision", "reason"), "decisions", empty=("reason",)),
        "verification": records(value["verification"], ("command", "result"), "verification", empty=("command",)),
        "next_steps": _strings(value["next_steps"], "next_steps"),
        "relevant_files": records(value["relevant_files"], ("path", "reason"), "relevant_files", empty=("reason",)),
    }
    return result, discarded


def render_compaction_summary(summary: dict) -> str:
    """结构控制栏目，正文保持模型内容，不根据结构推断事实可信度。"""
    def bullets(values: list[str]) -> str:
        return "\n".join("- " + value.replace("\n", "\n  ") for value in values) if values else "- (none)"

    findings = [f"[{entry['certainty']}] {entry['summary']}"
                + (f"\nEvidence: {entry['basis']}" if entry["basis"] else "")
                for entry in summary["findings"]]
    decisions = [entry["decision"] + (f"\nReason: {entry['reason']}" if entry["reason"] else "")
                 for entry in summary["decisions"]]
    verification = [(f"{entry['command']}: " if entry["command"] else "") + entry["result"]
                    for entry in summary["verification"]]
    files = [entry["path"] + (f": {entry['reason']}" if entry["reason"] else "")
             for entry in summary["relevant_files"]]
    sections = [
        "## Goal\n" + bullets([summary["goal"]]),
        "## Constraints & Preferences\n" + bullets(summary["constraints"]),
        "## Progress\n### Done\n" + bullets(summary["progress"]["done"])
        + "\n### In Progress\n" + bullets(summary["progress"]["in_progress"])
        + "\n### Blocked\n" + bullets(summary["progress"]["blocked"]),
        "## Findings\n" + bullets(findings),
        "## Key Decisions\n" + bullets(decisions),
        "## Verification\n" + bullets(verification),
        "## Next Steps\n" + bullets(summary["next_steps"]),
        "## Relevant Files\n" + bullets(files),
    ]
    return "\n\n".join(sections)
