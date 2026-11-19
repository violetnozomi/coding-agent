"""Tool: bash - Run shell commands with bounded durable execution progress."""

import codecs
from collections import deque
import os
import re
import shlex
import shutil
import tempfile
import time
from itertools import islice
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.foundation.workspace_paths import (
    WorkspacePathError,
    WorkspacePathPolicy,
)
from nz_coder.protocol.public_error import PublicRuntimeError, format_public_error
from nz_coder.runtime.core.execution_context import (
    strict_local_tools,
)
from nz_coder.runtime.process.platform_runtime import (
    select_shell,
)
from nz_coder.runtime.process.bash_execution import BashExecution
from nz_coder.runtime.process.command_channel import current_command_client
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.sessions import active_session_id
from nz_coder.tool_platform.artifacts import ArtifactError, ArtifactStore
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


class _RecoverableCommandOutput:
    """Spool a bounded continuous prefix before model-visible projection."""

    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, limit: int = MAX_BYTES, encoding: str = "") -> None:
        self.limit = max(1, min(self.MAX_BYTES, int(limit)))
        self._file = tempfile.TemporaryFile(mode="w+b")
        self._encoding = encoding or "utf-8"
        self.total_bytes = 0
        self.truncated = False
        self._closed = False

    def feed(self, payload: bytes) -> None:
        if self._closed:
            return
        data = bytes(payload)
        self.total_bytes += len(data)
        remaining = self.limit - self._file.tell()
        if remaining > 0:
            self._file.write(data[:remaining])
        if len(data) > max(0, remaining):
            self.truncated = True

    def finish(self, *, stream_complete: bool) -> tuple[str, bool]:
        if self._closed:
            return "", False
        self._file.flush()
        self._file.seek(0)
        payload = self._file.read()
        self._file.close()
        self._closed = True
        try:
            text = payload.decode(self._encoding, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        return text, bool(stream_complete and not self.truncated)

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True


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


def _resolve_bash_workdir(workdir: str | None, *, workspace: Path | None = None) -> tuple[Path | None, str]:
    """Resolve an optional command cwd without permitting workspace escape."""
    workspace = workspace or current_workdir()
    value = str(workdir or "").strip()
    try:
        candidate = WorkspacePathPolicy(workspace).validate_model_execute(value or ".")
        if not candidate.exists():
            return None, f"Error: workdir does not exist: {workdir}"
        if not candidate.is_dir():
            return None, f"Error: workdir is not a directory: {workdir}"
    except WorkspacePathError as exc:
        if "escapes workspace" in str(exc):
            return None, "Error: workdir escapes workspace"
        return None, format_public_error(exc)
    except (OSError, RuntimeError, ValueError):
        return None, "Error: Cannot resolve command workdir."
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


def validate_bash_timeout(timeout: object, maximum: int) -> str | None:
    """在审批及创建命令资源前统一校验本次运行的超时参数。"""
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return "Error: timeout must be an integer"
    if not 1 <= timeout <= maximum:
        return f"Error: timeout must be between 1 and {maximum}s"
    return None


def normalize_bash_command(command: str) -> str:
    """审批和实际启动复用同一个严格模式规范化，避免批准旧命令后再改写。"""
    if strict_local_tools():
        from nz_coder.swebench.policy import normalize_strict_bash_command

        return normalize_strict_bash_command(command)
    return command


def execute_bash_local(
    command: str,
    read_only: bool = False,
    timeout: int = None,
    workdir: str | None = None,
    *, create_execution=BashExecution, environment=None,
) -> str:
    from nz_coder.tool_platform.command_policy import assess_shell_command
    from nz_coder.tool_platform.permissioning.command_authorization import (
        authorization_failure, command_authorization_present, command_is_authorized,
    )

    settings = current_run_settings()
    timeout_error = validate_bash_timeout(timeout, settings.bash_timeout)
    if timeout_error:
        return timeout_error
    timeout_seconds = settings.bash_timeout if timeout is None else timeout
    requested_command = command
    command = normalize_bash_command(command)
    strict_output_filter_removed = command != requested_command
    resolved_workdir, workdir_error = _resolve_bash_workdir(workdir)
    if workdir_error:
        return workdir_error
    assert resolved_workdir is not None
    assessment = assess_shell_command(
        command, current_workdir(), command_cwd=resolved_workdir,
        read_only=read_only, allow_package_installs=settings.allow_package_installs,
    )
    if assessment.rejection:
        return f"Error: {assessment.rejection}"
    if (assessment.requires_approval or command_authorization_present()) and not command_is_authorized("bash", {
        "command": requested_command, "read_only": read_only,
        "timeout": timeout, "workdir": workdir,
    }):
        return authorization_failure("Denied: Command authorization is missing or no longer matches this invocation.")
    if strict_local_tools():
        from nz_coder.swebench.policy import strict_bash_guidance, strict_bash_violation

        violation = strict_bash_violation(command)
        if violation:
            guidance = strict_bash_guidance(command, violation)
            return f"Error: {violation}. {guidance}"
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []  # 无法解析的命令不声称已知可执行文件身份。
    if strict_local_tools():
        from nz_coder.intelligence.verification_planner import verification_command_segments

        for segment in verification_command_segments(command):
            if len(segment) < 2 or segment[1].removeprefix("./") != "bin/test":
                continue
            try:
                runner = WorkspacePathPolicy(current_workdir()).validate_model_execute(
                    str(resolved_workdir / segment[1]),
                )
            except WorkspacePathError as exc:
                return format_public_error(exc)
            if not runner.is_file():
                return "Error: bin/test is not a workspace file in bash.workdir"
    pythonpath_root = _strict_pytest_source_root(command, resolved_workdir)
    from nz_coder.runtime.process.execution_environment import current_execution_environment
    selected_environment = environment or current_execution_environment()
    try:
        process_environment = selected_environment.as_dict() if selected_environment else build_sanitized_subprocess_env()
    except PublicRuntimeError as exc:
        return format_public_error(exc)
    if pythonpath_root is not None:
        inherited = str(process_environment.get("PYTHONPATH") or "").strip()
        process_environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(pythonpath_root),
            inherited,
        )))
    strict_pythonpath_injected = pythonpath_root is not None
    command_identity = {
        "requested_command": requested_command,
        "executed_command": command,
        "workdir": str(resolved_workdir),
        # 这是启动配置的解析结果；不把它冒充由进程上报的解释器版本。
        "resolved_executable": shutil.which(tokens[0], path=process_environment.get("PATH")) if tokens else None,
    }
    title = _command_title(command)
    description = title
    try:
        shell = select_shell()
    except RuntimeError as exc:
        return format_public_error(exc)
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
            **command_identity,
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

    output_buffer = _BoundedCommandOutput(
        settings.process_buffer_bytes,
        settings.bash_output_hard_limit_bytes,
        settings.process_output_encoding,
    )
    recoverable_output = _RecoverableCommandOutput(
        encoding=settings.process_output_encoding or "utf-8",
    )
    last_report = 0.0

    def on_output(chunk: bytes) -> bool:
        nonlocal last_report
        recoverable_output.feed(chunk)
        if not output_buffer.limit_exceeded:
            output_buffer.feed(chunk)
        now = time.monotonic()
        if now - last_report >= 0.1:
            report_tool_metadata(
                title=title,
                metadata={
                    **command_identity,
                    "output": output_buffer.render(progress_limit).strip(),
                    "description": description,
                    "shell_kind": shell.kind.value,
                    "strict_output_filter_removed": strict_output_filter_removed,
                    "strict_pythonpath_injected": strict_pythonpath_injected,
                    "pythonpath_root": str(pythonpath_root or ""),
                },
            )
            last_report = now
        return not output_buffer.limit_exceeded

    from nz_coder.runtime.process.python_bytecode import capture_python_identity

    try:
        with capture_python_identity(shell, command, tokens, process_environment, resolved_workdir,
                                     current_workdir(), command_identity) as (body, descriptors):
            execution = create_execution(
                tuple(shell.argv(body, login=selected_environment is None)), cwd=resolved_workdir, env=process_environment,
                deadline=time.monotonic() + timeout_seconds,
                kill_grace=settings.process_kill_grace, on_output=on_output,
                **({"pass_fds": descriptors} if descriptors else {}),
            )
            outcome = execution.run(cancel_event=current_tool_cancel_event())
    except (FileNotFoundError, OSError) as exc:
        recoverable_output.close()
        return format_public_error(exc)
    except BaseException:
        recoverable_output.close()
        raise
    timed_out = outcome.timed_out
    cancelled = outcome.cancelled
    stream_complete = outcome.stream_complete
    output_buffer.finish()

    recoverable_text, recoverable_complete = recoverable_output.finish(
        stream_complete=stream_complete and not (timed_out or cancelled),
    )
    recovery_metadata: dict[str, object] = {}
    if recoverable_text and (
        output_buffer.truncated
        or output_buffer.limit_exceeded
        or not recoverable_complete
    ):
        try:
            artifact_id = ArtifactStore(
                current_workdir(),
                active_session_id() or "direct-tool",
            ).put(recoverable_text, kind="tool-result")
            recovery_metadata = {
                "raw_artifact_id": artifact_id,
                "raw_artifact_complete": recoverable_complete,
                "raw_artifact_bytes": len(recoverable_text.encode("utf-8")),
            }
        except (ArtifactError, OSError):
            recovery_metadata = {
                "raw_artifact_complete": False,
                "raw_artifact_error": "Artifact persistence failed",
            }
    elif output_buffer.truncated or output_buffer.limit_exceeded or not recoverable_complete:
        recovery_metadata = {
            "raw_artifact_complete": False,
            "raw_artifact_error": "No recoverable output was captured",
        }

    from nz_coder.runtime.adapters.verification_facts import command_receipt

    receipt = command_receipt(
        "bash", requested_command, str(resolved_workdir),
        outcome=("timed_out" if timed_out else "cancelled" if cancelled
                 else "failed" if outcome.exit_code != 0 else "succeeded"),
        exit_code=outcome.exit_code, output=recoverable_text,
        output_complete=bool(stream_complete and recoverable_complete
                             and not output_buffer.limit_exceeded and not timed_out and not cancelled),
        pipefail=shell.kind.value == "bash",
    )
    if timed_out:
        evidence = output_buffer.render(progress_limit).strip()
        suffix = f"\n{evidence}" if evidence else ""
        return ToolOutput(
            f"Error: Command timed out ({timeout_seconds}s){suffix}",
            title=title,
            execution_facts=(receipt,),
            metadata={
                **command_identity,
                "exit": None,
                "cancelled": False,
                "timed_out": True,
                "truncated": bool(output_buffer.truncated),
                "output_incomplete": True,
                **recovery_metadata,
            },
        )
    if cancelled:
        evidence = output_buffer.render(progress_limit).strip()
        suffix = f"\n{evidence}" if evidence else ""
        return ToolOutput(
            f"Error: Command cancelled{suffix}",
            title=title,
            execution_facts=(receipt,),
            metadata={
                **command_identity,
                "exit": None,
                "cancelled": True,
                "timed_out": False,
                "truncated": bool(output_buffer.truncated),
                "output_incomplete": True,
                **recovery_metadata,
            },
        )

    output = output_buffer.render().strip()
    if output_buffer.limit_exceeded:
        output = (
            "Command output limit exceeded; process terminated after "
            f"{output_buffer.total_bytes} bytes.\n{output}"
        )
    if outcome.exit_code != 0:
        prefix = f"Command exited with code {outcome.exit_code}"
        output = f"{prefix}\n{output}" if output else prefix
    if not output:
        output = f"({command.split()[0] if command.split() else 'bash'} completed with no output)"
    truncated = output_buffer.truncated or len(output) > config.CONTEXT_TRUNCATE_CHARS
    return ToolOutput(
        output,
        title=title,
        execution_facts=(receipt,),
        metadata={
            **command_identity,
            "output": _truncate_output(output, progress_limit),
            "exit": int(outcome.exit_code or 0),
            "description": description,
            "workdir": str(resolved_workdir),
            "shell_kind": shell.kind.value,
            "truncated": truncated,
            "total_output_bytes": output_buffer.total_bytes,
            "retained_output_bytes": output_buffer.retained_chars,
            "output_limit_exceeded": output_buffer.limit_exceeded,
            "output_incomplete": bool(
                output_buffer.limit_exceeded
                or not stream_complete
                or not recoverable_complete
                or recovery_metadata.get("raw_artifact_complete") is False
            ),
            "executed_command": command,
            "requested_command": requested_command,
            "strict_output_filter_removed": strict_output_filter_removed,
            "strict_pythonpath_injected": strict_pythonpath_injected,
            "pythonpath_root": str(pythonpath_root or ""),
            **recovery_metadata,
        },
    )


def run_bash(
    command: str, read_only: bool = False, timeout: int = None,
    workdir: str | None = None,
) -> str:
    timeout_error = validate_bash_timeout(timeout, current_run_settings().bash_timeout)
    if timeout_error:
        return timeout_error
    client = current_command_client()
    if client is not None:
        return client.call("bash", {
            "command": command, "read_only": read_only,
            "timeout": timeout, "workdir": workdir,
        }, cancel_event=current_tool_cancel_event())
    return execute_bash_local(command, read_only, timeout, workdir)


register(
    name="bash",
    description=("Run a shell command in the workspace. When individual check results matter, use one call "
                 "per check with its workdir so each has its own exit status. Use current verified results when applicable."),
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
