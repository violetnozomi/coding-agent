"""Bounded shell facts and closed-vocabulary diagnostics, never raw exceptions.

Shell streams are merged. Recognized compiler/test facts are reconstructed from
an allowlist; arbitrary messages, paths, source lines and headers stay hidden.
This is deliberately not a general-purpose secret-detection heuristic.
"""
from __future__ import annotations

import re


_SYNTAX = {
    "SyntaxError: invalid syntax": "syntax",
    "IndentationError: unexpected indent": "indent",
    "IndentationError: expected an indented block": "block",
    "SyntaxError: '(' was never closed": "unclosed_paren",
}
_LABELS = {
    "syntax": "SyntaxError{location}: invalid syntax",
    "indent": "IndentationError{location}: unexpected indent",
    "block": "IndentationError{location}: expected an indented block",
    "unclosed_paren": "SyntaxError{location}: '(' was never closed",
    "assertion": "AssertionError: assertion failed (details hidden)",
}


def _number(value: object, *, signed: bool = False) -> int | None:
    if type(value) is int and (-2**31 if signed else 0) <= value <= 2**63 - 1:
        return value
    return None


def capture_shell_diagnostic(output: str) -> dict:
    """Extract only fixed diagnostic categories/counts from the retained tail."""
    items = []
    line_number = None
    for line in output[-16_000:].splitlines():
        location = re.fullmatch(r'  File ".{1,1000}", line ([0-9]{1,9})(?:, in .*)?', line)
        if location:
            line_number = int(location[1])
        kind = _SYNTAX.get(line)
        if kind:
            items.append({"kind": kind, "line": line_number})
            line_number = None
        if re.fullmatch(r"AssertionError(?:: .{0,4000})?", line):
            items.append({"kind": "assertion", "line": None})
        failed = re.fullmatch(r"FAILED \(failures=([0-9]{1,9})(?:, errors=([0-9]{1,9}))?\)", line)
        if failed:
            items.append({"kind": "unittest", "failures": int(failed[1]),
                          "errors": int(failed[2] or 0)})
        pytest_summary = re.fullmatch(
            r"=* *((?:[0-9]{1,9} (?:failed|passed|skipped|warnings?|errors?)(?:, )?)+) in [0-9.]+s(?: \([^\r\n]{0,30}\))? *=*", line,
        )
        if pytest_summary:
            counts = dict((kind, int(count)) for count, kind in re.findall(
                r"([0-9]{1,9}) (failed|passed|skipped|warnings?|errors?)", pytest_summary[1]))
            if counts.get("failed") or counts.get("error") or counts.get("errors"):
                items.append({"kind": "pytest", "failures": counts.get("failed", 0),
                              "errors": counts.get("error", counts.get("errors", 0))})
    return {"status": "available" if items else "hidden" if output.strip() else "unavailable",
            "items": items[-4:]}


def shell_output_facts(metadata: object) -> dict:
    """Validate additive public metadata, including untrusted/legacy wire input."""
    source = metadata if isinstance(metadata, dict) else {}
    result = {"exit": _number(source.get("exit"), signed=True),
              "truncated": source.get("truncated") if type(source.get("truncated")) is bool else None}
    for key in ("total_output_bytes", "retained_output_bytes"):
        value = _number(source.get(key))
        if value is not None:
            result[key] = value
    if type(source.get("output_limit_exceeded")) is bool:
        result["output_limit_exceeded"] = source["output_limit_exceeded"]
    if source.get("termination") in ("timeout", "cancelled"):
        result["termination"] = source["termination"]
    diagnostic = source.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    items = []
    raw_items = diagnostic.get("items")
    for item in raw_items[:4] if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if isinstance(kind, str) and kind in _LABELS:
            items.append({"kind": kind, "line": _number(item.get("line"))})
        elif kind in ("unittest", "pytest"):
            failures, errors = _number(item.get("failures")), _number(item.get("errors"))
            if failures is not None and errors is not None:
                items.append({"kind": kind, "failures": failures, "errors": errors})
    status = diagnostic.get("status")
    status = status if status in ("hidden", "unavailable") else "unknown"
    result["diagnostic"] = {"status": "available" if items else status, "items": items}
    return result


def shell_failure_text(metadata: object, *, infrastructure: bool = False) -> str:
    """Render facts identically for model replies, events, snapshots and cards."""
    facts = shell_output_facts(metadata)
    exit_code = facts["exit"]
    label = "Tool infrastructure failure" if infrastructure else "Command failed"
    if facts.get("termination") == "timeout":
        label = "Command timed out"
    elif facts.get("termination") == "cancelled":
        label = "Command cancelled"
    lines = [f"{label}; exit code {exit_code if exit_code is not None else 'unknown'}."]
    truncated = facts["truncated"]
    lines.append("Output truncated (merged stdout/stderr)." if truncated is True
                 else "Output not truncated (merged stdout/stderr)." if truncated is False
                 else "Truncation unknown.")
    if facts.get("output_limit_exceeded") is True:
        lines.append("Command output limit exceeded; process terminated.")
    diagnostic = facts["diagnostic"]
    if diagnostic["status"] != "available":
        lines.append(f"Diagnostic {diagnostic['status']}; raw diagnostic not exposed.")
    else:
        lines.append("Safe diagnostic summary (other output hidden):")
        for item in diagnostic["items"]:
            if item["kind"] == "unittest":
                lines.append(f"unittest: {item['failures']} failures, {item['errors']} errors.")
            elif item["kind"] == "pytest":
                lines.append(f"pytest: {item['failures']} failed, {item['errors']} errors.")
            else:
                location = f" (line {item['line']})" if item["line"] is not None else ""
                lines.append(_LABELS[item["kind"]].format(location=location))
    return "\n".join(lines)
