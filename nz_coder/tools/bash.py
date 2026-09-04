"""Tool: bash - Run shell commands with bounded durable execution progress."""

import codecs
from collections import deque
import os
import queue
import re
import subprocess
import threading
import time
from itertools import islice
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.foundation.workspace_paths import (
    WorkspacePathError,
    WorkspacePathPolicy,
    model_command_private_path,
)
from nz_coder.runtime.core.execution_context import (
    broad_tests_blocked,
    declared_test_scopes,
    strict_local_tools,
)
from nz_coder.runtime.process.platform_runtime import (
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


class _BoundedCommandOutput:
    """Incrementally decode output while retaining only fixed head and tail windows."""

    def __init__(self, capacity: int, hard_limit: int, encoding: str = "") -> None:
        self.capacity = max(1024, int(capacity))
        self.hard_limit = max(self.capacity, int(hard_limit))
        self.head_limit = max(1, int(self.capacity * 0.6))
        self.tail_limit = max(1, self.capacity - self.head_limit)
        try:
            decoder_type = codecs.getincrementaldecoder(encoding or "utf-8")
        except LookupError:
            decoder_type = codecs.getincrementaldecoder("utf-8")
        self._decoder = decoder_type(errors="replace")
        self._initial: list[str] = []
        self._initial_chars = 0
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self.total_bytes = 0
        self.truncated = False
        self.limit_exceeded = False
        self._finished = False

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            return
        payload = bytes(chunk)
        self.total_bytes += len(payload)
        text = self._decoder.decode(payload, final=False)
        self._retain(text)
        if self.total_bytes > self.hard_limit:
            self.limit_exceeded = True

    def finish(self) -> None:
        if self._finished:
            return
        self._retain(self._decoder.decode(b"", final=True))
        self._finished = True

    @property
    def retained_chars(self) -> int:
        if not self.truncated:
            return self._initial_chars
        return len(self._head) + self._tail_chars

    def render(self, limit: int | None = None) -> str:
        if not self.truncated:
            value = "".join(self._initial)
        else:
            tail = "".join(self._tail)
            omitted = max(0, self.total_bytes - self.retained_chars)
            value = (
                self._head
                + f"\n\n... [{omitted} bytes omitted] ...\n\n"
                + tail
            )
        return _truncate_output(value, limit) if limit is not None else value

    def _retain(self, text: str) -> None:
        if not text:
            return
        if not self.truncated and self._initial_chars + len(text) <= self.capacity:
            self._initial.append(text)
            self._initial_chars += len(text)
            return
        if not self.truncated:
            combined = "".join(self._initial) + text
            self._initial.clear()
            self._initial_chars = 0
            self._head = combined[:self.head_limit]
            self.truncated = True
            self._append_tail(combined[self.head_limit:])
            return
        self._append_tail(text)

    def _append_tail(self, text: str) -> None:
        self._tail.append(text)
        self._tail_chars += len(text)
        while self._tail_chars > self.tail_limit and self._tail:
            excess = self._tail_chars - self.tail_limit
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                self._tail_chars -= len(first)
            else:
                self._tail[0] = first[excess:]
                self._tail_chars -= excess
                break


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
        content = WorkspaceFileAccess(current_workdir()).read_text(
            file_path, errors="replace",
        )
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
    try:
        candidate = WorkspacePathPolicy(workspace).validate_model_execute(value or ".")
    except WorkspacePathError as exc:
        if "escapes workspace" in str(exc):
            return None, "Error: workdir escapes workspace"
        return None, f"Error: {exc}"
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
    private_path = model_command_private_path(command, current_workdir())
    if private_path is not None:
        return f"Error: Model access blocked for shell path: {private_path}"
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

    settings = current_run_settings()
    classification = classify_bash(command)
    if classification["dangerous"]:
        return f"Error: Dangerous command blocked ({classification['reason']})"
    if classification["reason"] in {"package install", "package manager write"} and not settings.allow_package_installs:
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
        timeout_seconds = int(timeout or settings.bash_timeout)
    except (TypeError, ValueError, OverflowError):
        return "Error: timeout must be an integer"
    if timeout_seconds < 1 or timeout_seconds > settings.bash_timeout:
        return f"Error: timeout must be between 1 and {settings.bash_timeout}s"
    resolved_workdir, workdir_error = _resolve_bash_workdir(workdir)
    if workdir_error:
        return workdir_error
    assert resolved_workdir is not None
    pythonpath_root = _strict_pytest_source_root(command, resolved_workdir)
    process_environment = build_sanitized_subprocess_env()
    if pythonpath_root is not None:
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

    output_queue: queue.Queue = queue.Queue(maxsize=64)
    finished = object()
    reader_stop = threading.Event()

    def read_output() -> None:
        try:
            if process.stdout is not None:
                while not reader_stop.is_set():
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    while not reader_stop.is_set():
                        try:
                            output_queue.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
        finally:
            while not reader_stop.is_set():
                try:
                    output_queue.put(finished, timeout=0.1)
                    break
                except queue.Full:
                    continue

    reader = threading.Thread(target=read_output, name="nz-bash-output", daemon=True)
    reader.start()
    output_buffer = _BoundedCommandOutput(
        settings.process_buffer_bytes,
        settings.bash_output_hard_limit_bytes,
        settings.process_output_encoding,
    )
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
        output_buffer.feed(chunk)
        if output_buffer.limit_exceeded:
            _stop_process(process)
            break
        now = time.monotonic()
        if now - last_report >= 0.1:
            preview = output_buffer.render(progress_limit).strip()
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

    reader_stop.set()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        process.wait()
    reader.join(timeout=1)
    if reader.is_alive() and process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
        reader.join(timeout=1)
    while True:
        try:
            item = output_queue.get_nowait()
        except queue.Empty:
            break
        if item is not finished and not output_buffer.limit_exceeded:
            output_buffer.feed(
                item if isinstance(item, bytes) else str(item).encode("utf-8")
            )
    output_buffer.finish()

    if timed_out:
        evidence = output_buffer.render(progress_limit).strip()
        suffix = f"\n{evidence}" if evidence else ""
        return f"Error: Command timed out ({timeout_seconds}s){suffix}"
    if cancelled:
        evidence = output_buffer.render(progress_limit).strip()
        suffix = f"\n{evidence}" if evidence else ""
        return f"Error: Command cancelled{suffix}"

    output = output_buffer.render().strip()
    if output_buffer.limit_exceeded:
        output = (
            "Command output limit exceeded; process terminated after "
            f"{output_buffer.total_bytes} bytes.\n{output}"
        )
    if process.returncode != 0:
        prefix = f"Command exited with code {process.returncode}"
        output = f"{prefix}\n{output}" if output else prefix
    if not output:
        output = f"({command.split()[0] if command.split() else 'bash'} completed with no output)"
    truncated = output_buffer.truncated or len(output) > config.CONTEXT_TRUNCATE_CHARS
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
            "total_output_bytes": output_buffer.total_bytes,
            "retained_output_bytes": output_buffer.retained_chars,
            "output_limit_exceeded": output_buffer.limit_exceeded,
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
