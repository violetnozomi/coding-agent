"""Interactive permission prompt helpers."""
from __future__ import annotations

import json
from collections.abc import Callable


def format_tool_summary(tool_name: str, tool_input: dict) -> str:
    """Build a human-readable summary for permission prompts."""
    if tool_name == "doom_loop":
        repeated_tool = str(tool_input.get("tool") or "tool")
        return f"doom_loop: allow repeated identical call to {repeated_tool}?"
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        return f"bash: {str(cmd)[:2000]}"
    if tool_name == "process":
        operation = str(tool_input.get("operation") or "operation")
        detail = tool_input.get("command") or tool_input.get("process_id") or ""
        return f"process {operation}: {str(detail)[:120]}"
    if tool_name in ("write_file", "edit_file", "replace_lines", "apply_patch"):
        path = tool_input.get("path", "")
        if not path and tool_name == "apply_patch":
            changes = tool_input.get("changes", [])
            paths = [change.get("path", "") for change in changes if isinstance(change, dict)]
            path = ", ".join(value for value in paths if value)
        return f"{tool_name}: {path}"
    if tool_name == "python_structural_edit":
        path = tool_input.get("path", "")
        targets = []
        for replacement in tool_input.get("replacements", []):
            if isinstance(replacement, dict):
                targets.append(replacement.get("target", ""))
        return f"python_structural_edit: {path} — {', '.join(targets) or '(insertions)'}"
    if tool_name in ("save_memory", "delete_memory"):
        name = tool_input.get("name", "")
        return f"{tool_name}: {name}"
    preview = json.dumps(tool_input, ensure_ascii=False)[:150]
    return f"{tool_name}: {preview}"


def read_permission_answer(
    summary: str,
    renderer=None,
    tty_input: Callable[[str], str] | None = None,
) -> str | None:
    """Prompt the user until a valid permission answer is received."""
    prompt = "  Allow? (y/n/a=always/p=always-prefix): "
    console = getattr(renderer, "console", None)
    if console and hasattr(console, "print"):
        from nz_coder.interface.run_renderer import render_permission_request

        render_permission_request(console, summary)
    else:
        print(f"\n  [Permission] {summary}")

    readers: list[Callable[[], str]] = []
    if console and hasattr(console, "input"):
        readers.append(lambda: console.input(prompt, markup=False))
    readers.append(lambda: input(prompt))
    if tty_input is not None:
        readers.append(lambda: tty_input(prompt))

    valid_answers = {"y", "yes", "n", "no", "a", "p"}
    for _attempt in range(10):
        got_input = False
        for reader in readers:
            try:
                raw = reader()
            except KeyboardInterrupt:
                return None
            except (EOFError, OSError):
                continue
            if raw is None:
                continue
            answer = raw.strip().lower()
            got_input = True
            if answer in valid_answers:
                return answer
            break
        if console and hasattr(console, "print"):
            console.print("  Please answer y/n/a/p.", markup=False, highlight=False)
        else:
            print("  Please answer y/n/a/p.")
        if not got_input:
            continue
    return None
