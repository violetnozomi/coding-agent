"""Tool: bash - Run shell commands with durable execution progress."""

import os
import queue
import re
import subprocess
import threading
import time
from itertools import islice
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.runtime.core.execution_context import (
    broad_tests_blocked,
    declared_test_scopes,
    strict_local_tools,
)
from nz_coder.runtime.process.platform_runtime import (
    decode_process_output,
    select_shell,
    terminate_process_tree,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tool_platform.command_policy import (
    classify_bash,
    external_workspace_path,
    is_known_read_only_command,
)
from nz_coder.runtime.execution.runtime_state import _is_broad_test_command
from nz_coder.runtime.agent.task_policy import test_command_within_scopes
from nz_coder.tools import (
    ToolOutput,
    current_tool_cancel_event,
    register,
    report_tool_metadata,
)


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


def _command_title(command: str) -> str:
    """Return a compact single-line description for Session consumers."""
    compact = " ".join(str(command).split())
    return compact[:120] + ("…" if len(compact) > 120 else "")


def _stop_process(process: subprocess.Popen) -> None:
    """Best-effort terminate the shell and its child process group."""
    terminate_process_tree(process, force=True)


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

    fp = (current_workdir() / file_path).resolve()
    try:
        fp.relative_to(current_workdir().resolve())
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
    rel = str(fp.relative_to(current_workdir()))
    result = write_file(rel, new_content)
    return f"[sed intercepted → edit_file]\n{result}"


def _resolve_bash_workdir(workdir: str | None) -> tuple[Path | None, str]:
    """Resolve an optional command cwd without permitting workspace escape."""
    workspace = current_workdir().resolve()
    value = str(workdir or "").strip()
    candidate = Path(value) if value else workspace
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return None, "Error: workdir escapes workspace"
    if not candidate.exists():
        return None, f"Error: workdir does not exist: {workdir}"
    if not candidate.is_dir():
        return None, f"Error: workdir is not a directory: {workdir}"
    return candidate, ""


def _strict_pytest_source_root(command: str, workdir: Path) -> Path | None:
    """Return a local src-layout root that strict pytest should import first."""
    if not strict_local_tools() or re.match(
        r"^\s*(?:(?:python|python3)\s+-m\s+pytest|pytest|py\.test)\b",
        str(command or ""),
        flags=re.IGNORECASE,
    ) is None:
        return None
    workspace = current_workdir().resolve()
    for candidate in (workdir / "src", workspace / "src"):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            continue
        if resolved.is_dir() and _contains_python_source(resolved):
            return resolved
    return None


def _contains_python_source(root: Path) -> bool:
    """Boundedly recognize modules or namespace packages below a src root."""
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while pending and visited < 512:
        directory, depth = pending.pop(0)
        try:
            children = list(islice(directory.iterdir(), 256))
        except OSError:
            continue
        for child in children:
            visited += 1
            if child.is_file() and child.suffix.casefold() in {".py", ".pyi"}:
                return True
            if child.is_dir() and depth < 2 and visited < 512:
                pending.append((child, depth + 1))
    return False


def run_bash(
    command: str,
    read_only: bool = False,
    timeout: int = None,
    workdir: str | None = None,
) -> str:
    requested_command = command
    strict_output_filter_removed = False
    if strict_local_tools():
        from nz_coder.swebench.policy import normalize_strict_bash_command

        command = normalize_strict_bash_command(command)
        strict_output_filter_removed = command != requested_command
    escaped = external_workspace_path(command, current_workdir())
    if escaped is not None:
        return f"Error: Command path escapes workspace: {escaped}"
    if strict_local_tools():
        from nz_coder.swebench.policy import strict_bash_guidance, strict_bash_violation

        violation = strict_bash_violation(command)
        if violation:
            guidance = strict_bash_guidance(command, violation)
            return f"Error: {violation}. {guidance}"
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
    if (
        _is_broad_test_command(command)
        and broad_tests_blocked()
        and not test_command_within_scopes(command, declared_test_scopes())
    ):
        return (
            "Error: Broad test runner blocked. A source diff already exists. "
            "Use verify_changed_files or run an exact/narrow test command "
            "if the task points to a specific failure."
        )

    if read_only and (classification["mutating"] or not is_known_read_only_command(command)):
        return f"Error: Read-only shell blocked ({classification['reason']})"
    try:
        timeout_seconds = int(timeout or config.BASH_TIMEOUT_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return "Error: timeout must be an integer"
    if timeout_seconds < 1 or timeout_seconds > config.BASH_TIMEOUT_SECONDS:
        return f"Error: timeout must be between 1 and {config.BASH_TIMEOUT_SECONDS}s"
    resolved_workdir, workdir_error = _resolve_bash_workdir(workdir)
    if workdir_error:
        return workdir_error
    assert resolved_workdir is not None
    pythonpath_root = _strict_pytest_source_root(command, resolved_workdir)
    process_environment = None
    if pythonpath_root is not None:
        process_environment = dict(os.environ)
        inherited = str(process_environment.get("PYTHONPATH") or "").strip()
        process_environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(pythonpath_root),
            inherited,
        )))
    strict_pythonpath_injected = pythonpath_root is not None
    title = _command_title(command)
    description = title
    try:
        shell = select_shell()
    except RuntimeError as exc:
        return f"Error: {exc}"
    if shell.kind.value == "sh" and "|" in command:
        from nz_coder.intelligence.verification_planner import classify_verification_command

        if classify_verification_command(command):
            return (
                "Error: Verification pipelines require Bash pipefail, but only sh "
                "is available. Run the verification command directly without a "
                "pipeline; NZ-Coder will truncate or spill long output safely."
            )
    progress_limit = min(4000, config.CONTEXT_TRUNCATE_CHARS)
    report_tool_metadata(
        title=title,
        metadata={
            "output": "",
            "description": description,
            "workdir": str(resolved_workdir),
            "shell_kind": shell.kind.value,
            "executed_command": command,
            "requested_command": requested_command,
            "strict_output_filter_removed": strict_output_filter_removed,
            "strict_pythonpath_injected": strict_pythonpath_injected,
            "pythonpath_root": str(pythonpath_root or ""),
        },
    )

    try:
        process = subprocess.Popen(
            shell.argv(command),
            shell=False,
            cwd=resolved_workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
            env=process_environment,
        )
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    output_queue: queue.Queue = queue.Queue()
    finished = object()

    def read_output() -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    output_queue.put(line)
        finally:
            output_queue.put(finished)

    reader = threading.Thread(target=read_output, name="nz-bash-output", daemon=True)
    reader.start()
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_seconds
    last_report = 0.0
    timed_out = False
    cancelled = False
    cancel_event = current_tool_cancel_event()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _stop_process(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _stop_process(process)
            break
        try:
            item = output_queue.get(timeout=min(0.1, remaining))
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                break
            continue
        if item is finished:
            break
        # Production ``Popen`` is binary.  Keep compatibility with lightweight
        # embedders/tests that inject a text stream without weakening the real
        # raw-byte decoding contract.
        chunk = item if isinstance(item, bytes) else str(item).encode("utf-8")
        chunks.append(chunk)
        now = time.monotonic()
        if now - last_report >= 0.1:
            decoded = decode_process_output(
                b"".join(chunks),
                preferred_encoding=config.PROCESS_OUTPUT_ENCODING,
            )
            preview = _truncate_output(decoded.strip(), progress_limit)
            report_tool_metadata(
                title=title,
                metadata={
                    "output": preview,
                    "description": description,
                    "workdir": str(resolved_workdir),
                    "shell_kind": shell.kind.value,
                    "executed_command": command,
                    "requested_command": requested_command,
                    "strict_output_filter_removed": strict_output_filter_removed,
                    "strict_pythonpath_injected": strict_pythonpath_injected,
                    "pythonpath_root": str(pythonpath_root or ""),
                },
            )
            last_report = now

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        process.wait()
    reader.join(timeout=1)
    while True:
        try:
            item = output_queue.get_nowait()
        except queue.Empty:
            break
        if item is not finished:
            chunks.append(bytes(item))

    if timed_out:
        return f"Error: Command timed out ({timeout_seconds}s)"
    if cancelled:
        return "Error: Command cancelled"

    output = decode_process_output(
        b"".join(chunks),
        preferred_encoding=config.PROCESS_OUTPUT_ENCODING,
    ).strip()
    if process.returncode != 0:
        prefix = f"Command exited with code {process.returncode}"
        output = f"{prefix}\n{output}" if output else prefix
    if not output:
        output = f"({command.split()[0] if command.split() else 'bash'} completed with no output)"
    truncated = len(output) > config.CONTEXT_TRUNCATE_CHARS
    return ToolOutput(
        output,
        title=title,
        metadata={
            "output": _truncate_output(output, progress_limit),
            "exit": int(process.returncode or 0),
            "description": description,
            "workdir": str(resolved_workdir),
            "shell_kind": shell.kind.value,
            "truncated": truncated,
            "executed_command": command,
            "requested_command": requested_command,
            "strict_output_filter_removed": strict_output_filter_removed,
            "strict_pythonpath_injected": strict_pythonpath_injected,
            "pythonpath_root": str(pythonpath_root or ""),
        },
    )


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
            "workdir": {
                "type": "string",
                "description": (
                    "Workspace-relative directory in which to run the command. "
                    "Use this instead of cd. Defaults to the workspace root."
                ),
            },
        },
        "required": ["command"],
    },
    handler=run_bash,
    side_effect="mutates-shell",
)
