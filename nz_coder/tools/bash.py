"""Tool: bash - Run shell commands."""

import re
import subprocess

from nz_coder import config
from nz_coder.command_policy import classify_bash, is_known_read_only_command
from nz_coder.runtime_state import _is_broad_test_command, _is_exact_test
from nz_coder.tools import register


def _truncate_output(text: str, limit: int) -> str:
    """Middle-truncate: keep head and tail so both context and errors are visible."""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return (
        text[:half]
        + f"\n\n... [{omitted} characters omitted] ...\n\n"
        + text[-half:]
    )


# ── sed -i interception ───────────────────────────────────────────────────────
# When the model uses `sed -i 's/old/new/' file`, we intercept and route it
# through edit_file so the change is:
#   - visible as a diff in tool output
#   - tracked by the change tracker
#   - covered by transaction rollback
#   - subject to the same permission checks as other file edits
# Only simple single-file substitution commands (s/…/…/) are intercepted;
# complex sed scripts fall through to normal bash execution.

_SED_INPLACE_RE = re.compile(
    r"""^\s*sed\s+                   # sed command
    (?P<flags>(?:-[iEr]\S*\s+)*)     # optional flags like -i -E -r -i.bak
    (?P<expr>'[^']*'|"[^"]*"|\S+)    # the sed expression (quoted or bare)
    \s+(?P<file>\S+)\s*$             # the file path
    """,
    re.VERBOSE,
)


def _parse_sed_inplace(command: str):
    """Return (file_path, pattern, replacement, global_flag) or None.

    Only handles: sed [-i[.bak]] [-E] 's/PAT/REPL/[g]' FILE
    Multi-file, -e, piped, and non-substitution sed commands return None.
    """
    m = _SED_INPLACE_RE.match(command)
    if not m:
        return None

    flags_str = m.group("flags") or ""
    if "-i" not in flags_str and not re.search(r"-i\S*", flags_str):
        return None  # no in-place flag

    expr_raw = m.group("expr").strip("'\"")
    file_path = m.group("file")

    # Only handle substitution: s/PAT/REPL/[flags]
    sub_m = re.match(r"^s(.)(.+?)\1(.*?)\1([giIM]*)$", expr_raw)
    if not sub_m:
        return None

    pattern = sub_m.group(2)
    replacement = sub_m.group(3)
    sub_flags = sub_m.group(4)
    global_flag = "g" in sub_flags

    return file_path, pattern, replacement, global_flag


def _apply_sed_via_edit(command: str) -> str | None:
    """Try to intercept a sed -i command and apply it via edit_file.

    Returns the edit_file output string on success, or None if the command
    should fall through to normal bash execution.
    """
    parsed = _parse_sed_inplace(command)
    if parsed is None:
        return None

    file_path, pattern, replacement, global_flag = parsed

    from pathlib import Path
    fp = (config.WORKDIR / file_path).resolve()
    try:
        fp.relative_to(config.WORKDIR.resolve())
    except ValueError:
        return None  # path escapes workspace, let bash handle (and block) it

    if not fp.exists():
        return None  # file doesn't exist, let bash produce the real error

    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Apply substitution
    try:
        new_content = re.sub(
            pattern,
            replacement.replace("\\n", "\n"),
            content,
            count=0 if global_flag else 1,
        )
    except re.error:
        return None  # invalid regex, fall through to bash

    if new_content == content:
        return f"(sed: no changes — pattern not found in {file_path})"

    # Delegate to edit_file to get diff output + change tracking
    from nz_coder.tools.files import write_file
    rel = str(fp.relative_to(config.WORKDIR))
    result = write_file(rel, new_content)
    return f"[sed intercepted → edit_file]\n{result}"


def run_bash(command: str, read_only: bool = False, timeout: int = None) -> str:
    # Block sed -i: it silently writes files, bypassing transaction tracking,
    # verification gate, and RuntimeState edit counting.  Force the model to
    # use edit_file or replace_lines instead.
    if not read_only and _parse_sed_inplace(command) is not None:
        return (
            "Error: sed -i is blocked. It modifies files outside the edit-tool "
            "pipeline (transaction, verification, change tracking). "
            "Use edit_file with old_string/new_string for exact text replacement, "
            "or replace_lines for line-range edits."
        )

    classification = classify_bash(command)
    if classification["dangerous"]:
        return f"Error: Dangerous command blocked ({classification['reason']})"
    if classification["reason"] in {"package install", "package manager write"} and not config.ALLOW_BASH_PACKAGE_INSTALLS:
        return (
            "Error: Package install blocked. The agent must not modify the Python/"
            "Node/Ruby environment during benchmark repair. Use existing dependencies, "
            "py_compile, or a narrower in-repo verification command instead."
        )
    # ── Broad test blocking（当已有 source diff 时阻止跑全套测试）───────────
    if _is_broad_test_command(command) and getattr(config, "BLOCK_BROAD_TESTS", False):
        return (
            "Error: Broad test runner blocked. A source diff already exists. "
            "Use verify_changed_files or run an exact/narrow test command "
            "if the task points to a specific failure."
        )

    if read_only and (classification["mutating"] or not is_known_read_only_command(command)):
        return f"Error: Read-only shell blocked ({classification['reason']})"
    try:
        timeout_seconds = int(timeout or config.BASH_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return "Error: timeout must be an integer"
    if timeout_seconds < 1 or timeout_seconds > config.BASH_TIMEOUT_SECONDS:
        return f"Error: timeout must be between 1 and {config.BASH_TIMEOUT_SECONDS}s"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out ({timeout_seconds}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        prefix = f"Command exited with code {result.returncode}"
        output = f"{prefix}\n{output}" if output else prefix
    if not output:
        output = f"({command.split()[0] if command.split() else 'bash'} completed with no output)"
    return _truncate_output(output, config.CONTEXT_TRUNCATE_CHARS)


register(
    name="bash",
    description="Run a shell command in the workspace. Use for running tests, installing packages, git operations, etc.",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds, 1-{config.BASH_TIMEOUT_SECONDS}. Default: {config.BASH_TIMEOUT_SECONDS}.",
            },
        },
        "required": ["command"],
    },
    handler=run_bash,
)
