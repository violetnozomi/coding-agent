"""Error recovery: retry transient failures and inject targeted diagnostics."""
from __future__ import annotations


import json
import hashlib
import math
import re
import shlex
import time
import traceback
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from uuid import uuid4
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath

from nz_coder.foundation.error_classification import is_context_overflow_error
from nz_coder.runtime.verification.domain.models import TaskScope

_MAX_RETRY_AFTER_SECONDS = 120.0


@dataclass(frozen=True)
class RepeatSource:
    task: TaskScope
    interaction_run_id: str
    instruction_message_id: str
    authority_epoch: int
    active_agent_name: str
    observer_epoch: int
    mutation_generation: int


@dataclass(frozen=True)
class RepeatNotice:
    notice_id: str
    source: RepeatSource
    tool_name: str
    executed_repeat_count: int
    result_relation: str
    text: str


def _runner_token_escapes_workspace(token: str) -> bool:
    """Return whether a runner selector or option value names outside paths."""
    values = [str(token or "")]
    if "=" in values[0]:
        values.append(values[0].split("=", 1)[1])
    for value in values:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in PurePosixPath(normalized).parts
        ):
            return True
    return False


def _direct_repository_runner_tokens(
    tool_name: str,
    tool_input: dict | None,
) -> list[str]:
    """Parse one direct workspace-local Python ``tests/runtests.py`` call."""
    if tool_name != "bash":
        return []
    command = str((tool_input or {}).get("command") or "").strip()
    if not command or re.search(r"[;&|<>`$]", command):
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    command_index = 0
    if tokens[:1] == ["PYTHONPATH=."]:
        command_index = 1
    if len(tokens) < command_index + 2:
        return []
    if re.fullmatch(
        r"python(?:\d+(?:\.\d+)*)?",
        tokens[command_index],
        flags=re.IGNORECASE,
    ) is None:
        return []
    runner = tokens[command_index + 1].replace("\\", "/")
    if runner not in {"tests/runtests.py", "./tests/runtests.py"}:
        return []
    if any(
        _runner_token_escapes_workspace(token)
        for token in tokens[command_index + 2:]
    ):
        return []
    return tokens


def repository_test_runner_recovery_command(
    tool_name: str,
    output: str,
    *,
    tool_input: dict | None,
) -> str:
    """Return one evidenced local-runner retry command, or an empty string.

    This intentionally recognizes only a direct Python launch of the
    repository's ``tests/runtests.py``.  The narrow contract prevents shell
    fragments from being replayed as recovery guidance.
    """
    tokens = _direct_repository_runner_tokens(tool_name, tool_input)
    if not tokens:
        return ""

    from nz_coder.runtime.process.workdir import current_workdir

    workspace = current_workdir().resolve()
    runner_path = workspace / "tests" / "runtests.py"
    try:
        runner_path.resolve().relative_to(workspace)
    except (OSError, ValueError):
        return ""
    if not runner_path.is_file():
        return ""

    text = str(output or "")
    module = re.search(
        r"(?:ModuleNotFoundError:\s*)?No module named ['\"]([A-Za-z_]\w*)",
        text,
    )
    if module is not None:
        package = workspace / module.group(1)
        package_init = package / "__init__.py"
        try:
            package.resolve().relative_to(workspace)
            package_init.resolve().relative_to(workspace)
        except (OSError, ValueError):
            return ""
        if not package.is_dir() or not package_init.is_file():
            return ""
        if tokens[:1] == ["PYTHONPATH=."]:
            return ""
        return shlex.join(["PYTHONPATH=.", *tokens])

    if "testresult has no addduration method" not in text.casefold():
        return ""
    django_init = workspace / "django" / "__init__.py"
    try:
        django_init.resolve().relative_to(workspace)
    except (OSError, ValueError):
        return ""
    if not django_init.is_file():
        return ""

    recovered = list(tokens)
    for index, token in enumerate(recovered):
        if token == "--parallel":
            if index + 1 < len(recovered) and recovered[index + 1] == "1":
                return ""
            if index + 1 < len(recovered) and not recovered[index + 1].startswith("-"):
                recovered[index + 1] = "1"
            else:
                recovered.insert(index + 1, "1")
            return shlex.join(recovered)
        if token.startswith("--parallel="):
            if token == "--parallel=1":
                return ""
            recovered[index] = "--parallel=1"
            return shlex.join(recovered)
    recovered.extend(("--parallel", "1"))
    return shlex.join(recovered)


def _bounded_retry_after_seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _milliseconds_to_seconds(value: object) -> float | None:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return milliseconds / 1000.0

def _status_code(error: Exception | None) -> int | None:
    for owner in (error, getattr(error, "response", None)):
        for key in ("status_code", "status"):
            value = getattr(owner, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _response_headers(error: Exception | None) -> dict[str, object]:
    candidates = (
        getattr(error, "headers", None),
        getattr(error, "response_headers", None),
        getattr(getattr(error, "response", None), "headers", None),
    )
    for value in candidates:
        if value is None:
            continue
        try:
            return {str(key).lower(): item for key, item in dict(value).items()}
        except (TypeError, ValueError):
            continue
    return {}


class RecoveryState:
    def __init__(self):
        self.consecutive_errors = 0
        self.last_error = None
        self.max_retries = 3
        self.backoff_base = 2.0
        self._last_tool_signature: str | None = None
        self._last_tool_name: str | None = None
        self.repeated_tool_calls = 0
        self.tool_streak_resets = 0
        self._pending_tool_streak_event: dict | None = None
        self._last_tool_result_signature: str | None = None
        self._last_tool_result_digest: str | None = None
        self._observer_epoch = 0
        self._repeat_source: RepeatSource | None = None
        self._executed_repeat_count = 0
        self._repeat_notice: RepeatNotice | None = None
        self._repeat_notice_delivered = False
        # Edit recovery is run-scoped and keyed by the normalized workspace
        # path.  It intentionally lives beside the existing recovery counters
        # instead of introducing a second state manager.
        self.edit_recovery_attempts: dict[str, int] = {}
        self.blocked_edit_writes: set[str] = set()
        self._edit_recovery_versions: dict[str, dict] = {}

    @staticmethod
    def _edit_path(path: str) -> str:
        from nz_coder.runtime.process.workdir import current_workdir

        raw = str(path or "").strip().replace("\\", "/")
        if not raw:
            return ""
        try:
            root = current_workdir().resolve()
            target = (root / raw).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            return ""
        return str(target)

    def clear_edit_recovery(self, path: str) -> None:
        """Clear only the resolved file's failed-anchor state after success."""
        resolved = self._edit_path(path)
        if not resolved:
            return
        self.edit_recovery_attempts.pop(resolved, None)
        self.blocked_edit_writes.discard(resolved)
        self._edit_recovery_versions.pop(resolved, None)

    def edit_write_blocked(self, path: str) -> bool:
        """Return whether an existing file still has an unresolved edit failure."""
        resolved = self._edit_path(path)
        if not resolved or resolved not in self.blocked_edit_writes:
            return False
        try:
            stat = Path(resolved).stat()
        except OSError:
            self.clear_edit_recovery(path)
            return False
        expected = self._edit_recovery_versions.get(resolved) or {}
        current = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        if expected and any(current.get(key) != expected.get(key) for key in current):
            # A user or another run changed the file; the old anchor evidence
            # is stale and must not impose a permanent whole-file-write block.
            self.clear_edit_recovery(path)
            return False
        return True

    def edit_recovery_diagnostic(self, metadata: dict) -> str:
        """Record one real edit failure and render bounded recovery evidence."""
        if not isinstance(metadata, dict):
            return ""
        path = str(metadata.get("path") or "")
        resolved = self._edit_path(path)
        code = str(metadata.get("code") or "EDIT_NOT_FOUND")
        if not resolved:
            return ""
        attempt = self.edit_recovery_attempts.get(resolved, 0) + 1
        self.edit_recovery_attempts[resolved] = attempt
        version = metadata.get("version") if isinstance(metadata.get("version"), dict) else {}
        if metadata.get("exists") is True:
            self.blocked_edit_writes.add(resolved)
            self._edit_recovery_versions[resolved] = {
                "size": version.get("size"),
                "mtime_ns": version.get("mtime_ns"),
            }
        lines = [
            "<edit-recovery>",
            f"classification: {code.casefold()}",
            f"file: {path}",
            f"attempt: {attempt}",
        ]
        if code == "EDIT_TOO_LARGE":
            lines.extend((
                "The edit payload exceeded the safe edit limit.",
                "Split the change into smaller precise edits; do not replace the existing file wholesale.",
            ))
        elif code == "EDIT_AMBIGUOUS":
            lines.extend((
                "The anchor matched multiple locations.",
                "Expand it with distinguishing surrounding lines; do not shorten the anchor.",
            ))
        else:
            lines.extend((
                "The exact anchor was not found in the current file.",
                "Re-read the current file and submit one precise unique edit; do not write the whole file.",
            ))
        candidates = metadata.get("candidates")
        first_window: tuple[int, int] | None = None
        if isinstance(candidates, list):
            valid = [item for item in candidates[:3] if isinstance(item, dict)]
            for index, candidate in enumerate(valid):
                start = candidate.get("start_line")
                end = candidate.get("end_line")
                excerpt = str(candidate.get("excerpt") or "").strip()
                if not excerpt:
                    continue
                if first_window is None and isinstance(start, int) and isinstance(end, int):
                    first_window = (max(1, start), max(start, end))
                label = "candidate" if index == 0 else "alternative"
                lines.extend((
                    f"{label}_lines: {start}-{end}",
                    "```text",
                    excerpt[:1800],
                    "```",
                ))
        if not isinstance(candidates, list) or not any(
            isinstance(item, dict) and str(item.get("excerpt") or "").strip()
            for item in candidates
        ):
            lines.append("No reliable anchor candidate was found; use exact read_file output before editing.")
        read_offset, read_end = first_window or (1, 200)
        lines.extend((
            f"read_file: path={path} offset={read_offset} limit={max(1, read_end - read_offset + 1)}",
            f"file_version: size={version.get('size', '')} mtime_ns={version.get('mtime_ns', '')} hash={str(version.get('content_hash') or '')[:16]}",
            "</edit-recovery>",
        ))
        return "\n".join(lines)

    def record_success(self):
        self.consecutive_errors = 0
        self.last_error = None

    def record_error(self, error: Exception) -> dict:
        self.consecutive_errors += 1
        self.last_error = str(error)
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        retryable = self.is_retryable(error)
        return {
            "count": self.consecutive_errors,
            "error": str(error),
            "traceback": "".join(tb[-3:]),
            "should_retry": retryable and self.consecutive_errors <= self.max_retries,
            "should_abort": (not retryable) or self.consecutive_errors > self.max_retries,
        }

    def backoff_wait(self, error: Exception | None = None) -> float:
        if self.consecutive_errors <= 0:
            return 0.0
        wait = self.backoff_seconds(error)
        print(f"  [recovery] Waiting {wait:.0f}s before retry ({self.consecutive_errors}/{self.max_retries})...")
        time.sleep(wait)
        return wait

    def backoff_seconds(self, error: Exception | None = None) -> float:
        """Return the current provider-aware delay without sleeping."""
        header_wait = self.retry_after_seconds(error) if error is not None else None
        if header_wait is not None:
            return header_wait
        return min(self.backoff_base ** max(1, self.consecutive_errors), 30)

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        """Apply InfCode-style transient classification before backoff."""
        status = _status_code(error)
        text = str(error).lower()
        if status in {400, 401, 403, 404, 422}:
            return False
        if is_context_overflow_error(error):
            return False
        if any(word in text for word in ("freeusagelimiterror", "invalid api key", "authentication")):
            return False
        if status in {408, 409, 425, 429} or (status is not None and status >= 500):
            return True
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        return any(word in text for word in (
            "temporary", "timeout", "timed out", "connection", "overloaded",
            "unavailable", "rate limit", "too many requests", "try again",
        ))

    @staticmethod
    def retry_after_seconds(error: Exception | None) -> float | None:
        """Read Retry-After-Ms or Retry-After seconds/date from SDK errors."""
        headers = _response_headers(error)
        milliseconds = headers.get("retry-after-ms")
        if milliseconds is not None:
            delay = _bounded_retry_after_seconds(
                _milliseconds_to_seconds(milliseconds)
            )
            if delay is not None:
                return delay
        retry_after = headers.get("retry-after")
        if retry_after is None:
            return None
        delay = _bounded_retry_after_seconds(retry_after)
        if delay is not None:
            return delay
        try:
            parsed = parsedate_to_datetime(str(retry_after))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _bounded_retry_after_seconds(
                (parsed - datetime.now(timezone.utc)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _clear_repeat_evidence(self) -> None:
        self._last_tool_result_signature = None
        self._last_tool_result_digest = None
        self._repeat_source = None
        self._executed_repeat_count = 0
        self._repeat_notice = None
        self._repeat_notice_delivered = False

    def capture_repeat_source(self, state, run, active_agent_name, messages) -> RepeatSource | None:
        from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, is_user_request_message

        if run is None or state._verification_context is not run or not active_agent_name:
            return None
        with run._state_lock:
            if (run.verification is None or run.cancel_scope.is_set()
                    or run.finalized or run.terminal_status is not None):
                return None
            task = run.verification.scope.task
            user = next((m for m in reversed(messages) if is_user_request_message(m)), None)
            instruction = user.get(MESSAGE_ID_KEY, "") if user is not None else ""
            if not instruction:
                # 压缩和图切换可以移走真实指令；仅借用初始化时已核实的绑定，不能把合成消息当用户。
                binding = state.task_assessment_state.get("binding", {})
                if binding.get("task") == asdict(task) and binding.get("authority_epoch") == state.task_authority_epoch:
                    instruction = binding.get("instruction_message_id", "")
            if not instruction:
                return None
            return RepeatSource(task, run.interaction_run_id, instruction, state.task_authority_epoch,
                                active_agent_name, self._observer_epoch, state.mutation_generation)

    def repeat_source_current(self, source, state, run, active_agent_name, messages) -> bool:
        return source is not None and source == self.capture_repeat_source(state, run, active_agent_name, messages)

    def refresh_repeat_source_after_compaction(self, source, state, run, active_agent_name, messages):
        if source is None:
            return None
        candidate = replace(source, observer_epoch=self._observer_epoch)
        return candidate if self.repeat_source_current(candidate, state, run, active_agent_name, messages) else None

    def repeat_notice_snapshot(self, state, run, active_agent_name, messages) -> RepeatNotice | None:
        if run is None:
            return None
        with run._state_lock:
            notice = self._repeat_notice
            if (notice is not None and not self._repeat_notice_delivered
                    and self.repeat_source_current(notice.source, state, run, active_agent_name, messages)):
                return notice
            return None

    def acknowledge_repeat_notice(self, notice_id: str, source: RepeatSource) -> None:
        # 调用方在当前Run锁内验证来源；旧请求不能确认后来产生的新事件。
        notice = self._repeat_notice
        if notice is not None and notice.notice_id == notice_id and notice.source == source:
            self._repeat_notice_delivered = True

    def reset_tool_call_history(self, reason: str = "manual", *, context_changed: bool = False) -> None:
        """Reset consecutive identical-tool tracking and describe why."""
        if self._last_tool_signature is not None:
            self._record_tool_streak_reset(reason=reason, next_tool=None)
        self._last_tool_signature = None
        self._last_tool_name = None
        self.repeated_tool_calls = 0
        self._clear_repeat_evidence()
        if context_changed:
            self._observer_epoch += 1



    def start_tool_call_run(self) -> None:
        """Start per-run streak accounting without carrying previous-run statistics."""
        self._last_tool_signature = None
        self._last_tool_name = None
        self.repeated_tool_calls = 0
        self.tool_streak_resets = 0
        self._pending_tool_streak_event = None
        self._clear_repeat_evidence()
        self._observer_epoch += 1

    def consume_tool_streak_event(self) -> dict | None:
        """Return and clear the latest streak reset event for trace emission."""
        event = self._pending_tool_streak_event
        self._pending_tool_streak_event = None
        return event

    def record_tool_result_evidence(
        self, tool_name: str, tool_input: object, output: str, *,
        executed: bool, dispatch_failed: bool, command_failed: bool = False,
        cache_hit: bool = False, metadata: dict | None = None,
        source: RepeatSource | None = None,
    ) -> bool:
        """Reset a streak only when structured, relevant evidence changed."""
        if source is not None and source.observer_epoch != self._observer_epoch:
            return False
        encoded = json.dumps(
            tool_input, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        signature = f"{tool_name}\0{encoded}"
        # 批量预检先于结果回放；未执行/缓存的调用仍保留工具和参数切换边界，不能串起前后的旧证据。
        if signature != self._last_tool_result_signature or source != self._repeat_source:
            self._clear_repeat_evidence()
        if not executed or dispatch_failed or cache_hit:
            return False
        digest = self._tool_evidence_digest(
            output, command_failed=command_failed, metadata=metadata,
        )
        changed = (
            signature == self._last_tool_result_signature
            and digest != self._last_tool_result_digest
        )
        if changed:
            self.reset_tool_call_history(reason="new_tool_evidence")
        self._last_tool_result_signature = signature
        self._last_tool_result_digest = digest
        self._repeat_source = source
        self._executed_repeat_count += 1
        if source is not None and self._executed_repeat_count >= 2 and self._repeat_notice is None:
            name = str(tool_name)[:120]
            text = ("<repeat-observation>\nRuntime observation, not a user instruction or a verdict of no progress. "
                    f"Tool `{name}` with identical arguments executed {self._executed_repeat_count} times; "
                    "the normalized evidence digests are equal. This does not mean the full outputs are identical. "
                    "This may be a legitimate recheck. Consider the task and decide whether to continue, "
                    "adjust the approach, or update the working plan.\n</repeat-observation>")
            self._repeat_notice = RepeatNotice(uuid4().hex, source, name, self._executed_repeat_count,
                                               "normalized_evidence_equal", text)
        return changed

    @staticmethod
    def _tool_evidence_digest(
        output: str, *, command_failed: bool, metadata: dict | None,
    ) -> str:
        """Ignore volatile timing fields while retaining failure diagnostics."""
        data = metadata if isinstance(metadata, dict) else {}
        volatile = {"timestamp", "time", "elapsed", "elapsed_ms", "duration", "duration_ms", "wall_ms"}
        if command_failed:
            evidence = {
                "exit": data.get("exit"),
                "diagnostic": re.sub(
                    r"(?:\b\d{4}-\d{2}-\d{2}[T ][^\s]+|\b\d+(?:\.\d+)?\s*(?:ms|s|seconds))",
                    "<volatile>", str(data.get("output") or output or ""),
                    flags=re.IGNORECASE,
                ),
            }
        elif any(key in data for key in ("state", "status", "phase")):
            evidence = {
                key: value for key, value in data.items()
                if key not in volatile and key not in {"output", "preview"}
            }
        else:
            evidence = str(output or "")
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _record_tool_streak_reset(self, *, reason: str, next_tool: str | None) -> None:
        self.tool_streak_resets += 1
        self._pending_tool_streak_event = {
            "reason": reason,
            "previous_tool": self._last_tool_name,
            "previous_count": self.repeated_tool_calls,
            "next_tool": next_tool,
            "reset_count": self.tool_streak_resets,
        }

    def observe_tool_call(
        self,
        tool_name: str,
        tool_input: object,
        *,
        threshold: int,
    ) -> dict:
        """Track consecutive calls with the same tool name and arguments.

        Arguments are canonicalized so JSON key order does not bypass the guard.
        A non-positive threshold disables the guard.
        """
        if threshold <= 0:
            self.reset_tool_call_history(reason="guard_disabled")
            return {"count": 0, "should_block": False}

        encoded = json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        signature = f"{tool_name}\0{encoded}"
        same_as_previous = signature == self._last_tool_signature
        if same_as_previous:
            self.repeated_tool_calls += 1
        else:
            if self._last_tool_signature is not None:
                reason = "tool_changed" if tool_name != self._last_tool_name else "arguments_changed"
                self._record_tool_streak_reset(reason=reason, next_tool=tool_name)
            if signature != self._last_tool_result_signature:
                self._clear_repeat_evidence()
            self._last_tool_signature = signature
            self._last_tool_name = tool_name
            self.repeated_tool_calls = 1
        effective_threshold = max(2, threshold)
        consecutive_block = self.repeated_tool_calls >= effective_threshold
        return {
            "count": self.repeated_tool_calls,
            "should_block": consecutive_block,
        }

    # FIXED: 删除 inject_diagnostic —— 该方法在 loop.py 中从未被调用（死代码），
    # 且与 loop.py 里已有的 _make_client_error_diag / tool_failure_diagnostic 职责重叠。
    # 保留死代码会在代码审查中引起混淆（"这里应该调用但没调用么？"）。

    def tool_failure_diagnostic(
        self,
        tool_name: str,
        output: str,
        *,
        tool_input: dict | None = None,
        declared_paths: tuple[str, ...] = (),
        metadata: dict | None = None,
    ) -> str:
        """Return a targeted diagnostic for common coding-agent tool failures."""
        text = output or ""
        lower = text.lower()
        if (metadata or {}).get("stall_kind") == "consecutive":
            return (
                "<repeat-approval-diagnostic>\n"
                f"Tool `{tool_name}` was not executed: identical candidates reached the repeat approval boundary. "
                "This is a resource safeguard, not a finding of no progress. "
                "Respect the permission outcome and use the existing evidence to choose the next step.\n"
                "</repeat-approval-diagnostic>"
            )
        if tool_name in {"edit_file", "apply_patch"} and "old_text not found" in lower:
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` could not find the exact old_text.\n"
                "Do not retry from memory. Re-read the target file around the nearby context, "
                "copy the exact current snippet, then retry a smaller edit. For Python symbol-level "
                "changes, load the `python_ast` optional pack first, then prefer "
                "`python_structural_edit` over repeated exact-text edits. If you are only "
                "adding new content at end-of-file, use `apply_patch` with `op=append`, "
                "`path`, and `new_text`; omit `old_text` instead of guessing an anchor.\n"
                "</tool-failure-diagnostic>"
            )
        if tool_name in {"edit_file", "apply_patch"} and "old_text matches" in lower:
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` found multiple matches.\n"
                "Make the edit more specific by including more surrounding lines, or use a "
                "symbol-aware tool when editing Python code.\n"
                "</tool-failure-diagnostic>"
            )
        if "workdir escapes workspace" in lower:
            return (
                "<tool-failure-diagnostic>\n"
                "classification: workspace_boundary\n"
                f"Tool `{tool_name}` rejected an explicit working directory outside the "
                "active workspace. Retry the workspace-local command once and omit `workdir`; "
                "tools already default to the active workspace root. Do not inspect or target "
                "the outside path, and do not change source code to compensate for it.\n"
                "</tool-failure-diagnostic>"
            )
        workspace_boundary = _bash_workspace_boundary_diagnostic(
            tool_name,
            text,
        )
        if workspace_boundary:
            return workspace_boundary
        declared_artifact = _declared_artifact_path_diagnostic(
            tool_name,
            text,
            tool_input=tool_input,
            declared_paths=declared_paths,
        )
        if declared_artifact:
            return declared_artifact
        command_package_root = _command_package_root_diagnostic(
            tool_name,
            text,
            tool_input=tool_input,
        )
        if command_package_root:
            return command_package_root
        repository_runner_recovery = _repository_test_runner_recovery_diagnostic(
            tool_name,
            text,
            tool_input=tool_input,
        )
        if repository_runner_recovery:
            return repository_runner_recovery
        runner_mismatch = _repository_test_runner_mismatch_diagnostic(
            tool_name,
            text,
            tool_input=tool_input,
        )
        if runner_mismatch:
            return runner_mismatch
        if text.startswith("Command exited with code"):
            return _build_test_failure_diagnostic(text)
        if text.startswith("Denied"):
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` was denied by policy. Choose a safer, narrower command or "
                "use read/edit tools directly.\n"
                "</tool-failure-diagnostic>"
            )
        if text.startswith("Error:"):
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` failed. Use the error output as ground truth, inspect the "
                "current workspace state, and try a different approach.\n"
                "</tool-failure-diagnostic>"
            )
        return ""

    def verification_gate_message(self, last_verification: dict | None) -> str:
        """Message injected when the model tries to finish without a passing check."""
        if last_verification:
            output = last_verification.get('output', '')
            excerpt = _extract_failure_excerpt(output)
            detail = (
                f"Last verification command: `{last_verification.get('command', 'unknown')}`\n"
                f"Last verification status: {last_verification.get('status', 'unknown')}\n"
                f"Relevant output:\n{excerpt}"
            )
        else:
            detail = "No verification command has passed since the last file edit."
        return (
            "<verification-required>\n"
            "You edited files but have not produced a passing verification after the edit. "
            "Do not finish yet. Run the most specific relevant test or import/behavior check, "
            "debug any failure, and only finish after a passing check.\n\n"
            f"{detail}\n"
            "</verification-required>"
        )


# ── Module-level output parsing helpers ──────────────────────────────────────

def _extract_failed_tests(output: str) -> list[str]:
    """Extract failing test IDs from pytest/unittest output."""
    tests: list[str] = []
    seen: set[str] = set()
    # pytest: "FAILED tests/foo.py::Bar::test_baz - AssertionError"
    for m in re.finditer(r"FAILED\s+([\w/\\.\-]+(?:::\w+)+)", output):
        t = m.group(1)
        if t not in seen:
            seen.add(t)
            tests.append(t)
    # unittest: "FAIL: test_baz (tests.test_foo.Bar)"
    for m in re.finditer(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([^)]+)\)", output, re.MULTILINE):
        t = f"{m.group(2)}.{m.group(1)}"
        if t not in seen:
            seen.add(t)
            tests.append(t)
    return tests


def _extract_regression_tests(output: str, baseline_passed: set[str] | None = None) -> list[str]:
    """Identify tests that were passing before but now fail (regressions).

    Without a baseline we can only detect tests explicitly marked as regressions
    in the output (e.g. SWE-bench PASS_TO_PASS failures). When a baseline set is
    provided, any test in both failed_tests and baseline_passed is a regression.
    """
    failed = set(_extract_failed_tests(output))
    if baseline_passed:
        return sorted(failed & baseline_passed)
    # Fallback: look for SWE-bench-style PASS_TO_PASS labels
    regressions: list[str] = []
    for m in re.finditer(r"PASS_TO_PASS.*?FAILED\s+([\w/\\.\-]+(?:::\w+)+)", output):
        regressions.append(m.group(1))
    return regressions


def _extract_traceback(output: str, max_chars: int = 1500) -> str:
    """Extract the most relevant traceback fragment from test output.

    Strategy:
    1. Find the last 'Traceback (most recent call last)' block — it's the root cause.
    2. If none, look for 'FAILURES' section header (pytest).
    3. Fall back to the tail of the output.
    """
    # Last traceback block
    tb_starts = [m.start() for m in re.finditer(r"Traceback \(most recent call last\)", output)]
    if tb_starts:
        start = tb_starts[-1]
        fragment = output[start:start + max_chars]
        return fragment.strip()

    # pytest FAILURES section
    failures_idx = output.find("FAILURES")
    if failures_idx >= 0:
        fragment = output[failures_idx:failures_idx + max_chars]
        return fragment.strip()

    # AssertionError / Error lines as last resort
    for marker in ("AssertionError", "Error:", "FAILED"):
        idx = output.rfind(marker)
        if idx >= 0:
            start = max(0, idx - 200)
            return output[start:idx + max_chars].strip()

    return output[-max_chars:].strip()


def _extract_failure_excerpt(output: str, max_chars: int = 2000) -> str:
    """Extract the most useful failure excerpt for gate messages.

    Locates the FAILURES section or last traceback rather than blindly
    truncating the tail (which often contains only summary lines).
    """
    if not output:
        return "(no output)"

    # pytest FAILURES section
    failures_idx = output.find("FAILURES")
    if failures_idx >= 0:
        # Include a bit of context before the marker
        start = max(0, failures_idx - 100)
        return output[start:start + max_chars].strip()

    # Last traceback
    tb_starts = [m.start() for m in re.finditer(r"Traceback \(most recent call last\)", output)]
    if tb_starts:
        start = max(0, tb_starts[-1] - 100)
        return output[start:start + max_chars].strip()

    # Fall back to tail
    return output[-max_chars:].strip()


def _portable_workspace_cwd_hint(helper: str, workspace: str) -> str:
    """Return an exact helper-relative expression for the workspace root."""
    path = PurePosixPath(str(helper or "").replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return ""
    parent_index = len(path.parts) - 1
    expression = f"Path(__file__).resolve().parents[{parent_index}]"
    return (
        "For a portable repair in this exact helper, use "
        f"`{expression}`; it resolves to `{workspace}`. Add "
        "`from pathlib import Path` if needed, and do not translate it into "
        "guessed dirname calls."
    )


def _declared_artifact_path_diagnostic(
    tool_name: str,
    output: str,
    *,
    tool_input: dict | None,
    declared_paths: tuple[str, ...],
) -> str:
    """Redirect one failed read to a unique contract-owned basename."""
    if tool_name != "read_file" or "file not found" not in output.casefold():
        return ""
    requested = str((tool_input or {}).get("path") or "").strip().replace("\\", "/")
    requested_path = PurePosixPath(requested)
    if (
        not requested
        or requested_path.is_absolute()
        or ".." in requested_path.parts
        or re.match(r"^[A-Za-z]:/", requested)
    ):
        return ""
    matches: list[str] = []
    for raw in declared_paths:
        value = str(raw or "").strip().replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:/", value)
        ):
            continue
        normalized = path.as_posix()
        if path.name == requested_path.name and normalized not in matches:
            matches.append(normalized)
    if len(matches) != 1 or matches[0] == requested_path.as_posix():
        return ""
    target = matches[0]
    return (
        "<tool-failure-diagnostic>\n"
        "primary_classification: declared_artifact_path\n"
        f"repair_target: {target}\n"
        f"Tool `read_file` could not find `{requested}`. The Runtime TaskContract "
        f"declares the unique same-basename artifact `{target}`. Call `read_file` "
        "with that exact path now. Do not run glob or broad repository discovery "
        "for this already-resolved path.\n"
        "</tool-failure-diagnostic>"
    )


def _bash_workspace_boundary_diagnostic(tool_name: str, output: str) -> str:
    """Turn a denied stale absolute command path into an exact local retry."""
    lower = output.casefold()
    if (
        tool_name != "bash"
        or not output.startswith("Denied")
        or "path outside workspace" not in lower
    ):
        return ""
    from nz_coder.runtime.process.workdir import current_workdir

    workspace = current_workdir().resolve()
    return (
        "<tool-failure-diagnostic>\n"
        "classification: workspace_boundary\n"
        f"Active workspace root: `{workspace}`\n"
        "The command copied a stale absolute path outside this run's workspace. "
        "Retry the same workspace-local command once: remove the explicit `cd` "
        "and omit `workdir`, because bash already starts at the active workspace "
        "root shown above. Do not probe the stale path and do not change source "
        "code to compensate for it.\n"
        "</tool-failure-diagnostic>"
    )


def _command_package_root_diagnostic(
    tool_name: str,
    output: str,
    *,
    tool_input: dict | None,
) -> str:
    """Correct one direct Python import launched from inside its package dir."""
    if tool_name != "bash" or not output.startswith("Command exited with code"):
        return ""
    module = re.search(
        r"ModuleNotFoundError:\s+No module named ['\"]([A-Za-z_]\w*)",
        output,
    )
    workdir = str((tool_input or {}).get("workdir") or "").strip()
    if module is None or not workdir or workdir == ".":
        return ""
    from nz_coder.runtime.process.workdir import current_workdir

    workspace = current_workdir().resolve()
    package = module.group(1)
    package_dir = (workspace / package).resolve()
    command_dir = (workspace / workdir).resolve()
    try:
        command_dir.relative_to(workspace)
        package_dir.relative_to(workspace)
    except ValueError:
        return ""
    if command_dir != package_dir or not package_dir.is_dir():
        return ""
    return (
        "<tool-failure-diagnostic>\n"
        "classification: command_package_root\n"
        f"Active workspace root: `{workspace}`\n"
        f"Detected package directory: `{package}`\n"
        f"The direct Python command used workdir `{workdir}`, which is the package "
        "directory itself. This could explain the import failure if the containing "
        "directory is absent from this interpreter's import path. One candidate "
        "probe is to omit `workdir` (or use `.`) and compare the import result. "
        "Check the actual interpreter and package layout before choosing a repair.\n"
        "</tool-failure-diagnostic>"
    )


def _repository_test_runner_mismatch_diagnostic(
    tool_name: str,
    output: str,
    *,
    tool_input: dict | None,
) -> str:
    """Redirect a generic pytest launch to an evidenced native Django runner."""
    if tool_name != "bash" or not output.startswith("Command exited with code"):
        return ""
    command = str((tool_input or {}).get("command") or "")
    if re.search(
        r"(?:^|\s)(?:(?:python|pypy)\d*(?:\.\d+)*\s+-m\s+)?pytest(?:\s|$)",
        command,
        flags=re.IGNORECASE,
    ) is None:
        return ""
    lower = output.casefold()
    django_bootstrap_failure = (
        "object' object has no attribute 'databases'" in lower
        or 'object" object has no attribute "databases"' in lower
        or (
            "django.core.exceptions.improperlyconfigured" in lower
            and "settings" in lower
        )
    )
    if not django_bootstrap_failure:
        return ""

    from nz_coder.runtime.process.workdir import current_workdir

    runner = current_workdir().resolve() / "tests" / "runtests.py"
    if not runner.is_file():
        return ""
    target = _pytest_path_from_command(command)
    selector = _native_python_test_selector(target) if target else ""
    native_command = "python tests/runtests.py"
    if selector:
        native_command += " " + shlex.quote(selector)
    return (
        "<test-failure-diagnostic>\n"
        "classification: repository_test_runner_mismatch\n"
        "primary_classification: repository_test_runner_mismatch\n"
        "Detected repository runner: `tests/runtests.py`\n"
        "The output reports a Django settings/bootstrap failure under pytest. "
        "The repository runner could supply the expected setup. A candidate "
        f"command is `{native_command}`; verify its selector, options and environment "
        "against the repository instructions before using it. This diagnosis does "
        "not establish the repair location. A changed command does not replace "
        "the user's explicit acceptance command.\n"
        "</test-failure-diagnostic>"
    )


def _repository_test_runner_recovery_diagnostic(
    tool_name: str,
    output: str,
    *,
    tool_input: dict | None,
) -> str:
    """Render an exact retry for a recoverable local Django runner failure."""
    command = repository_test_runner_recovery_command(
        tool_name,
        output,
        tool_input=tool_input,
    )
    if not command:
        return ""
    parallelism_mismatch = (
        "testresult has no addduration method" in str(output or "").casefold()
    )
    classification = (
        "repository_runner_parallelism"
        if parallelism_mismatch
        else "repository_package_path"
    )
    explanation = (
        "The output reports missing addDuration support, which may indicate a "
        "mismatch between unittest and the runner's parallel result handling."
        if parallelism_mismatch
        else "The repository package exists in the active checkout, but the direct "
        "runner could not import it. The checkout root may be absent from the "
        "interpreter's import path."
    )
    return (
        "<test-failure-diagnostic>\n"
        f"classification: {classification}\n"
        f"primary_classification: {classification}\n"
        f"{explanation} A candidate probe is `{command}`. Check its test scope, "
        "options and environment before execution, then use the result to decide "
        "the next step. A changed command does not replace the user's explicit "
        "acceptance command.\n"
        "</test-failure-diagnostic>"
    )


def _pytest_path_from_command(command: str) -> str:
    """Return the first workspace-local Python test path from a pytest command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        normalized = token.replace("\\", "/").lstrip("./")
        path = normalized.split("::", 1)[0]
        if path.startswith("tests/") and path.endswith(".py"):
            return normalized
    return ""


def _native_python_test_selector(target: str) -> str:
    """Convert tests/foo/test_bar.py::Case into a runtests.py label."""
    raw_path, *nodes = str(target or "").replace("\\", "/").split("::")
    parts = [part for part in raw_path.lstrip("./").split("/") if part]
    if parts[:1] == ["tests"]:
        parts = parts[1:]
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1:] == ["__init__"]:
        parts.pop()
    return ".".join([*parts, *(node for node in nodes if node)])


def _build_test_failure_diagnostic(output: str) -> str:
    """Build a structured diagnostic from a failed bash command output."""
    from nz_coder.intelligence.failure_diagnostics import (
        DiagnosticSignal,
        render_failure_diagnostic,
    )

    failed_tests = _extract_failed_tests(output)
    traceback_text = _extract_traceback(output)
    regression_tests = _extract_regression_tests(output)
    lower = output.lower()
    import_collection_failure = (
        "modulenotfounderror" in lower
        and any(marker in lower for marker in (
            "while importing test module",
            "error collecting",
            "errors during collection",
        ))
    )
    subprocess_package_root_failure = (
        "completedprocess" in lower
        and "no module named" in lower
        and any(marker in lower for marker in ("python -m", "'-m'", '"-m"'))
    )

    failed_paths = list(dict.fromkeys(
        test.split("::", 1)[0].replace("\\", "/")
        for test in failed_tests
        if "::" in test
    ))
    try:
        from nz_coder.intelligence.subprocess_workspace import (
            diagnose_subprocess_workspace_drift,
        )
        from nz_coder.runtime.process.workdir import current_workdir

        workspace_drift = diagnose_subprocess_workspace_drift(
            output,
            workspace=current_workdir(),
        )
    except (OSError, RuntimeError, ValueError):
        workspace_drift = None
    signals: list[DiagnosticSignal] = []
    if workspace_drift is not None:
        portable_cwd = _portable_workspace_cwd_hint(
            workspace_drift.helper,
            str(workspace_drift.active_workspace),
        )
        signals.append(DiagnosticSignal(
            classification="subprocess_workspace_drift",
            specificity=100,
            repair_target=workspace_drift.helper,
            evidence=(
                f"Failing helper: `{workspace_drift.helper}:{workspace_drift.line}`\n"
                f"Resolved subprocess cwd: `{workspace_drift.resolved_cwd}`\n"
                f"Active workspace cwd: `{workspace_drift.active_workspace}`\n"
                f"Launched package: `{workspace_drift.package}`"
            ),
            action=(
                "Static inspection resolves this helper's `cwd` to a directory other "
                "than the active workspace. Check whether that location is intentional "
                "and which package the subprocess actually imports. Using the active "
                "workspace root is a possible repair; select a relevant check to verify it."
                + (f"\n{portable_cwd}" if portable_cwd else "")
            ),
        ))

    if subprocess_package_root_failure and workspace_drift is None:
        module_match = re.search(
            r"['\"]-m['\"]\s*,\s*['\"]([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)",
            output,
        )
        package_name = (
            module_match.group(1).split(".", 1)[0]
            if module_match is not None
            else "<package>"
        )
        try:
            from nz_coder.runtime.process.workdir import current_workdir

            workspace_root = str(current_workdir().resolve())
        except Exception:
            workspace_root = "<active-workspace-root>"
        failing = (
            "Failing tests:\n" + "\n".join(f"  - {t}" for t in failed_tests[:5])
            if failed_tests else ""
        )
        portable_cwd = _portable_workspace_cwd_hint(
            failed_paths[0] if failed_paths else "",
            workspace_root,
        )
        signals.append(DiagnosticSignal(
            classification="subprocess_package_root",
            specificity=90,
            evidence=failing,
            repair_target=failed_paths[0] if failed_paths else "",
            action=(
                "The output reports that a subprocess launched with `python -m <package>` "
                "could not import the package. Its interpreter, `cwd` or `env` could differ "
                "from the parent process. Inspecting the failing test's subprocess helper "
                "and the directory that contains the package can help distinguish these causes.\n"
                f"Active workspace root: `{workspace_root}`. Detected package directory: "
                f"`{package_name}`. If `{workspace_root}/{package_name}` exists, the layout is "
                "workspace-root/package. That root may be an appropriate `cwd`; for other layouts, "
                "verify the source and import paths before choosing a repair. Follow user "
                "authorization for any environment changes."
                + (f"\n{portable_cwd}" if portable_cwd else "")
            ),
        ))

    if import_collection_failure:
        signals.append(DiagnosticSignal(
            classification="import_or_package_layout",
            specificity=80,
            evidence=(f"Relevant import failure:\n{traceback_text}" if traceback_text else ""),
            action=(
                "Test collection failed before this output established behavior coverage. "
                "Possible causes include the interpreter, import path, missing dependencies "
                "or package declarations. A workspace-local probe using the same interpreter "
                "can report the working directory and attempt the failing import. Use that "
                "evidence to choose further investigation or a repair, respecting user "
                "authorization. This failure does not establish that implementation is complete."
            ),
        ))

    if (
        len(failed_paths) >= 2
        and not import_collection_failure
        and not subprocess_package_root_failure
    ):
        signals.append(DiagnosticSignal(
            classification="widespread_test_regression",
            specificity=50,
            evidence=(
                "Failures span multiple test files:\n"
                + "\n".join(f"  - {path}" for path in failed_paths[:6])
            ),
            action=(
                "Failures across files may share production code, test helpers, configuration "
                "or dependencies. Compare the failures and recent changes to locate a shared "
                "cause. Inspect shared production code or test setup as the evidence warrants, "
                "then choose representative checks. The number of failing files alone does "
                "not establish a regression or its repair location."
            ),
        ))

    if signals:
        return render_failure_diagnostic(signals)

    parts: list[str] = ["<test-failure-diagnostic>"]
    if regression_tests:
        parts.append(
            "Regressions reported by the test output:\n"
            + "\n".join(f"  - {t}" for t in regression_tests[:5])
        )
        parts.append(
            "Compare these failures with the target failure and recent changes to "
            "identify related causes and choose the repair order. Check existing "
            "behavior alongside the requested change when validating the repair."
        )
    elif failed_tests:
        parts.append(
            "Failing tests:\n" + "\n".join(f"  - {t}" for t in failed_tests[:5])
        )

    if traceback_text:
        parts.append(f"Traceback excerpt:\n{traceback_text}")

    parts.append(
        "Possible next steps:\n"
        "Compare the error and traceback with the test expectations, implementation "
        "and execution environment. Investigate the relevant files before choosing a "
        "repair, then run checks suited to the change. The output alone does not "
        "establish which file should be modified or that the task is complete."
    )
    parts.append("</test-failure-diagnostic>")
    return "\n\n".join(parts)
