"""Hook pipeline for runtime policies and configurable lifecycle automation."""
from __future__ import annotations

import copy
import fnmatch
import inspect
import json
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import is_synthetic_user_message, stamp_user_message
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.agent.task_policy import is_test_file, normalize_path as _policy_normalize_path
from nz_coder.tools.todo import get_reminder

log = logging.getLogger(__name__)

_KEEP_GOING_RE = re.compile(
    r"^(?:please\s+)?(?:keep\s+going|go\s+on|继续|keep\s+working)$"
    r"|^continue$",
    re.IGNORECASE,
)

_NEGATIVE_RE = re.compile(
    r"\b(wtf|wth|ffs|shit|damn\s+it|broken|useless|terrible|awful|horrible"
    r"|what\s+the\s+(fuck|hell)|so\s+frustrating|this\s+sucks|screw\s+this"
    r"|不对|不行|不对劲|怎么回事|搞什么)\b",
    re.IGNORECASE,
)

_VALID_CONFIG_HOOK_EVENTS = {
    "turn_start",
    "pre_send",
    "post_receive",
    "pre_tool_use",
    "post_tool_use",
    "turn_end",
    "no_tool_response",
}
_VALID_ACTION_TYPES = {"prompt"}
_VALID_HOOK_ERROR_POLICIES = {"ignore", "log", "prompt", "reject"}
_OPERATORS = ("==", "!=", "=~", "~=")

NoToolResponseHook = Callable[["NoToolResponseContext"], str | None]
StopHook = Callable[["StopHookContext"], Any]
ToolResultHook = Callable[["ToolResultContext"], None]
ToolBatchHook = Callable[["ToolBatchContext"], None]


class HookConfigError(Exception):
    """Raised when a configured hook is invalid."""


class HookConditionParseError(Exception):
    """Raised when a hook condition expression cannot be parsed."""


@dataclass
class HookCondition:
    field: str
    operator: str
    value: str

    def evaluate(self, ctx: "HookContext") -> bool:
        field_value = ctx.get_field(self.field)
        if self.operator == "==":
            return field_value == self.value
        if self.operator == "!=":
            return field_value != self.value
        if self.operator == "=~":
            pattern = self.value
            if pattern.startswith("/") and pattern.endswith("/"):
                pattern = pattern[1:-1]
            try:
                return bool(re.search(pattern, field_value))
            except re.error:
                return False
        if self.operator == "~=":
            return fnmatch.fnmatch(field_value, self.value)
        return False


@dataclass
class HookConditionGroup:
    conditions: list[HookCondition] = field(default_factory=list)
    logic: str = "and"

    def evaluate(self, ctx: "HookContext") -> bool:
        if not self.conditions:
            return True
        if self.logic == "and":
            return all(condition.evaluate(ctx) for condition in self.conditions)
        return any(condition.evaluate(ctx) for condition in self.conditions)


@dataclass
class HookAction:
    type: str
    message: str = ""


@dataclass
class ConfiguredHook:
    id: str
    event: str
    action: HookAction
    condition: HookConditionGroup | None = None
    reject: bool = False
    continue_run: bool = False
    on_error: str = "log"
    error_message: str = ""
    once: bool = False
    executed: bool = False

    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True

    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookDecision:
    rejected: bool = False
    continue_run: bool = False
    message: str = ""
    hook_id: str = ""


@dataclass
class NoToolResponseContext:
    loop: Any
    messages: list
    message: str = ""
    status: str = "completed"


@dataclass(frozen=True)
class StopHookContext:
    """Isolated natural-stop snapshot passed to InfCodeX-style stop hooks."""

    transcript: tuple[dict[str, Any], ...]
    last_assistant_text: str
    signal: str = "natural-end"
    reanimate_count: int = 0
    reanimate_budget: int = 2
    runtime_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StopHookDecision:
    """Normalized stop-hook result: complete, degraded, reanimate, or abort."""

    action: str = "complete"
    message: str = ""
    source: str = ""


@dataclass
class ToolResultContext:
    loop: Any
    messages: list
    result: Any
    output: str


@dataclass
class ToolBatchContext:
    loop: Any
    messages: list
    manual_compact: bool
    used_todo: bool
    on_text: Callable | None
    write_total: int
    write_denied: int


@dataclass
class HookContext:
    loop: Any
    messages: list
    event_name: str
    session_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    turn_count: int = 0
    message_count: int = 0
    task_mode: str = ""
    initial_task: str = ""
    last_user_message: str = ""
    last_assistant_message: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    requested_paths: list[str] = field(default_factory=list)
    requested_test_paths: list[str] = field(default_factory=list)
    missing_requested_paths: list[str] = field(default_factory=list)
    missing_requested_test_paths: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    actual_output_paths: list[str] = field(default_factory=list)
    created_test_files: list[str] = field(default_factory=list)
    modified_test_files: list[str] = field(default_factory=list)
    actual_test_output_paths: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    edits_this_run: int = 0
    has_diff: bool = False
    wants_tests: bool = False
    tests_modified: bool = False
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    normalized_file_path: str = ""
    file_basename: str = ""
    file_dir: str = ""
    first_requested_path: str = ""
    first_requested_test_path: str = ""
    first_missing_requested_path: str = ""
    first_missing_requested_test_path: str = ""
    conflicting_requested_path: str = ""
    message: str = ""
    output: str = ""
    status: str = ""
    error: str = ""
    is_write: bool = False
    is_test_file: bool = False
    requested_path_exact_match: bool = False
    requested_basename_match: bool = False
    same_basename_conflict: bool = False

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name == "session_id":
            return self.session_id
        if name == "agent_id":
            return self.agent_id
        if name == "trace_id":
            return self.trace_id
        if name in {"turn", "turn_count"}:
            return str(self.turn_count)
        if name == "message_count":
            return str(self.message_count)
        if name == "task_mode":
            return self.task_mode
        if name == "initial_task":
            return self.initial_task
        if name == "last_user_message":
            return self.last_user_message
        if name == "last_assistant_message":
            return self.last_assistant_message
        if name in {"acceptance_count", "acceptance_criteria_count"}:
            return str(len(self.acceptance_criteria))
        if name == "requested_paths_count":
            return str(len(self.requested_paths))
        if name == "requested_test_paths_count":
            return str(len(self.requested_test_paths))
        if name == "missing_requested_paths_count":
            return str(len(self.missing_requested_paths))
        if name == "missing_requested_test_paths_count":
            return str(len(self.missing_requested_test_paths))
        if name == "created_files_count":
            return str(len(self.created_files))
        if name == "modified_files_count":
            return str(len(self.modified_files))
        if name == "actual_output_paths_count":
            return str(len(self.actual_output_paths))
        if name == "created_test_files_count":
            return str(len(self.created_test_files))
        if name == "modified_test_files_count":
            return str(len(self.modified_test_files))
        if name == "actual_test_output_paths_count":
            return str(len(self.actual_test_output_paths))
        if name == "changed_files_count":
            return str(len(self.changed_files))
        if name == "edits_this_run":
            return str(self.edits_this_run)
        if name == "has_diff":
            return str(self.has_diff).lower()
        if name == "wants_tests":
            return str(self.wants_tests).lower()
        if name == "tests_modified":
            return str(self.tests_modified).lower()
        if name == "is_write":
            return str(self.is_write).lower()
        if name == "is_test_file":
            return str(self.is_test_file).lower()
        if name == "file_path":
            return self.file_path
        if name == "normalized_file_path":
            return self.normalized_file_path
        if name == "file_basename":
            return self.file_basename
        if name == "file_dir":
            return self.file_dir
        if name == "first_requested_path":
            return self.first_requested_path
        if name == "first_requested_test_path":
            return self.first_requested_test_path
        if name == "first_missing_requested_path":
            return self.first_missing_requested_path
        if name == "first_missing_requested_test_path":
            return self.first_missing_requested_test_path
        if name == "conflicting_requested_path":
            return self.conflicting_requested_path
        if name == "requested_path_exact_match":
            return str(self.requested_path_exact_match).lower()
        if name == "requested_basename_match":
            return str(self.requested_basename_match).lower()
        if name == "same_basename_conflict":
            return str(self.same_basename_conflict).lower()
        if name == "message":
            return self.message
        if name == "output":
            return self.output
        if name == "status":
            return self.status
        if name == "error":
            return self.error
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value is not None else ""
        return ""

    def expand(self, template: str) -> str:
        result = str(template or "")
        replacements = {
            "$EVENT": self.event_name,
            "$SESSION_ID": self.session_id,
            "$AGENT_ID": self.agent_id,
            "$TRACE_ID": self.trace_id,
            "$TURN_COUNT": str(self.turn_count),
            "$MESSAGE_COUNT": str(self.message_count),
            "$TASK_MODE": self.task_mode,
            "$INITIAL_TASK": self.initial_task,
            "$LAST_USER_MESSAGE": self.last_user_message,
            "$LAST_ASSISTANT_MESSAGE": self.last_assistant_message,
            "$ACCEPTANCE_CRITERIA": ", ".join(self.acceptance_criteria),
            "$REQUESTED_PATHS": ", ".join(self.requested_paths),
            "$REQUESTED_TEST_PATHS": ", ".join(self.requested_test_paths),
            "$MISSING_REQUESTED_PATHS": ", ".join(self.missing_requested_paths),
            "$MISSING_REQUESTED_TEST_PATHS": ", ".join(self.missing_requested_test_paths),
            "$CREATED_FILES": ", ".join(self.created_files),
            "$MODIFIED_FILES": ", ".join(self.modified_files),
            "$ACTUAL_OUTPUT_PATHS": ", ".join(self.actual_output_paths),
            "$CREATED_TEST_FILES": ", ".join(self.created_test_files),
            "$MODIFIED_TEST_FILES": ", ".join(self.modified_test_files),
            "$ACTUAL_TEST_OUTPUT_PATHS": ", ".join(self.actual_test_output_paths),
            "$CHANGED_FILES": ", ".join(self.changed_files),
            "$EDITS_THIS_RUN": str(self.edits_this_run),
            "$HAS_DIFF": str(self.has_diff).lower(),
            "$WANTS_TESTS": str(self.wants_tests).lower(),
            "$TESTS_MODIFIED": str(self.tests_modified).lower(),
            "$IS_WRITE": str(self.is_write).lower(),
            "$IS_TEST_FILE": str(self.is_test_file).lower(),
            "$TOOL_NAME": self.tool_name,
            "$FILE_PATH": self.file_path,
            "$NORMALIZED_FILE_PATH": self.normalized_file_path,
            "$FILE_BASENAME": self.file_basename,
            "$FILE_DIR": self.file_dir,
            "$FIRST_REQUESTED_PATH": self.first_requested_path,
            "$FIRST_REQUESTED_TEST_PATH": self.first_requested_test_path,
            "$FIRST_MISSING_REQUESTED_PATH": self.first_missing_requested_path,
            "$FIRST_MISSING_REQUESTED_TEST_PATH": self.first_missing_requested_test_path,
            "$CONFLICTING_REQUESTED_PATH": self.conflicting_requested_path,
            "$REQUESTED_PATH_EXACT_MATCH": str(self.requested_path_exact_match).lower(),
            "$REQUESTED_BASENAME_MATCH": str(self.requested_basename_match).lower(),
            "$SAME_BASENAME_CONFLICT": str(self.same_basename_conflict).lower(),
            "$MESSAGE": self.message,
            "$OUTPUT": self.output,
            "$STATUS": self.status,
            "$ERROR": self.error,
        }
        for key, value in replacements.items():
            result = result.replace(key, value)
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        return result


@dataclass
class AgentHooks:
    """Extensible hook registry for loop lifecycle events."""

    before_no_tool_response_hooks: list[NoToolResponseHook] = field(default_factory=list)
    stop_hooks: list[StopHook] = field(default_factory=list)
    stop_hook_reanimate_budget: int = 2
    after_tool_result_hooks: list[ToolResultHook] = field(default_factory=list)
    after_tool_batch_hooks: list[ToolBatchHook] = field(default_factory=list)
    configured_hooks: list[ConfiguredHook] = field(default_factory=list)
    _prompt_messages: list[str] = field(default_factory=list)
    _stop_hook_reanimate_count: int = 0
    _stop_hook_reason: str = ""

    def register_before_no_tool_response(self, hook: NoToolResponseHook) -> None:
        self.before_no_tool_response_hooks.append(hook)

    def register_stop_hook(self, hook: StopHook) -> None:
        """Register a bounded natural-stop interceptor."""
        self.stop_hooks.append(hook)

    def reset_run_state(self) -> None:
        """Reset counters that must not leak across independent Agent runs."""
        self._stop_hook_reanimate_count = 0
        self._stop_hook_reason = ""

    @property
    def stop_hook_reason(self) -> str:
        return self._stop_hook_reason

    def register_after_tool_result(self, hook: ToolResultHook) -> None:
        self.after_tool_result_hooks.append(hook)

    def register_after_tool_batch(self, hook: ToolBatchHook) -> None:
        self.after_tool_batch_hooks.append(hook)

    def register_configured_hook(self, hook: ConfiguredHook) -> None:
        self.configured_hooks.append(hook)

    def consume_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages

    def has_pre_tool_use_hooks(self) -> bool:
        return any(hook.event == "pre_tool_use" for hook in self.configured_hooks)

    def handle_no_tool_response(self, loop: Any, messages: list, *, message: str = "") -> str:
        ctx = NoToolResponseContext(loop=loop, messages=messages, message=message, status="completed")
        for hook in self.before_no_tool_response_hooks:
            hook_status = hook(ctx)
            if hook_status is not None:
                ctx.status = hook_status
            if ctx.status == "continue":
                return "continue"
        if ctx.status not in {"completed", "completed_unverified"}:
            return ctx.status
        decision = self._run_configured_event(
            self._base_context(loop, messages, "no_tool_response", message=message, status=ctx.status)
        )
        if decision is not None and decision.continue_run:
            return "continue"
        stop_status = self._run_stop_hooks(loop, messages, message=message)
        if stop_status is not None:
            return stop_status
        return ctx.status

    async def handle_no_tool_response_async(
        self,
        loop: Any,
        messages: list,
        *,
        message: str = "",
    ) -> str:
        """Async natural-stop path that can await LLM-judged consumers."""
        ctx = NoToolResponseContext(
            loop=loop,
            messages=messages,
            message=message,
            status="completed",
        )
        for hook in self.before_no_tool_response_hooks:
            hook_status = hook(ctx)
            if hook_status is not None:
                ctx.status = hook_status
            if ctx.status == "continue":
                return "continue"
        if ctx.status not in {"completed", "completed_unverified"}:
            return ctx.status
        decision = self._run_configured_event(
            self._base_context(
                loop,
                messages,
                "no_tool_response",
                message=message,
                status=ctx.status,
            )
        )
        if decision is not None and decision.continue_run:
            return "continue"
        stop_status = await self._run_stop_hooks_async(
            loop,
            messages,
            message=message,
        )
        if stop_status is not None:
            return stop_status
        return ctx.status

    async def _run_stop_hooks_async(
        self,
        loop: Any,
        messages: list,
        *,
        message: str,
    ) -> str | None:
        """Await async hooks while preserving ordered fail-open composition."""
        if not self.stop_hooks:
            return None
        budget = max(0, int(self.stop_hook_reanimate_budget))
        context = self._build_stop_hook_context(loop, messages, message, budget)
        for hook in self.stop_hooks:
            try:
                raw_decision = hook(context)
                if inspect.isawaitable(raw_decision):
                    raw_decision = await raw_decision
                decision = self._normalize_stop_hook_decision(raw_decision)
            except Exception as exc:
                tracer = getattr(loop, "tracer", None)
                if tracer is not None:
                    tracer.log(
                        "stop_hook_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                continue
            status = self._apply_stop_hook_decision(
                loop, messages, decision, budget=budget
            )
            if status is not None:
                return status
        return None

    def _run_stop_hooks(self, loop: Any, messages: list, *, message: str) -> str | None:
        """Run stop hooks with the same bounded reanimation contract as InfCodeX."""
        if not self.stop_hooks:
            return None
        budget = max(0, int(self.stop_hook_reanimate_budget))
        context = self._build_stop_hook_context(loop, messages, message, budget)
        for hook in self.stop_hooks:
            try:
                raw_decision = hook(context)
                if inspect.isawaitable(raw_decision):
                    close = getattr(raw_decision, "close", None)
                    if callable(close):
                        close()
                    raise TypeError("Async stop hook requires handle_no_tool_response_async")
                decision = self._normalize_stop_hook_decision(raw_decision)
            except Exception as exc:
                tracer = getattr(loop, "tracer", None)
                if tracer is not None:
                    tracer.log(
                        "stop_hook_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                continue
            status = self._apply_stop_hook_decision(
                loop, messages, decision, budget=budget
            )
            if status is not None:
                return status
        return None

    def _build_stop_hook_context(
        self,
        loop: Any,
        messages: list,
        message: str,
        budget: int,
    ) -> StopHookContext:
        runtime_state = getattr(loop, "runtime_state", None)
        runtime_snapshot = (
            runtime_state.to_dict(active=True)
            if runtime_state is not None and hasattr(runtime_state, "to_dict")
            else {}
        )
        terminal_ready = getattr(runtime_state, "strict_generation_terminal_ready", None)
        if callable(terminal_ready):
            runtime_snapshot["strict_generation_ready"] = bool(terminal_ready())
        verification_manager = getattr(loop, "vm", None)
        if verification_manager is not None and hasattr(verification_manager, "status"):
            runtime_snapshot["verification"] = verification_manager.status()
        return StopHookContext(
            transcript=tuple(copy.deepcopy(messages)),
            last_assistant_text=message,
            reanimate_count=self._stop_hook_reanimate_count,
            reanimate_budget=budget,
            runtime_state=copy.deepcopy(runtime_snapshot),
        )

    def _apply_stop_hook_decision(
        self,
        loop: Any,
        messages: list,
        decision: StopHookDecision,
        *,
        budget: int,
    ) -> str | None:
        if decision.action == "complete":
            return None
        if decision.action == "complete_unverified":
            return "completed_unverified"
        tracer = getattr(loop, "tracer", None)
        if decision.action == "abort":
            self._stop_hook_reason = decision.message or "Stopped by runtime stop hook."
            if tracer is not None:
                tracer.log(
                    "stop_hook_abort",
                    reason=self._stop_hook_reason,
                    source=decision.source,
                )
            return "stopped_by_hook"
        if self._stop_hook_reanimate_count >= budget:
            self._stop_hook_reason = (
                f"Stop-hook reanimate budget exhausted ({budget}/{budget})."
            )
            if tracer is not None:
                tracer.log(
                    "stop_hook_budget_exhausted",
                    count=self._stop_hook_reanimate_count,
                    budget=budget,
                    source=decision.source,
                )
            return "stopped_by_hook"
        prompt = decision.message.strip()
        if not prompt:
            return None
        self._stop_hook_reanimate_count += 1
        messages.append(stamp_user_message({
            "role": "user",
            "content": (
                "<stop-hook-guidance>\n"
                f"{prompt[:4000]}\n"
                "</stop-hook-guidance>"
            ),
            "_nz_synthetic": True,
            "_nz_stop_hook": True,
        }))
        if tracer is not None:
            tracer.log(
                "stop_hook_reanimate",
                count=self._stop_hook_reanimate_count,
                budget=budget,
                source=decision.source,
            )
        return "continue"

    @staticmethod
    def _normalize_stop_hook_decision(value: Any) -> StopHookDecision:
        if value is None:
            return StopHookDecision()
        if isinstance(value, StopHookDecision):
            if value.action not in {"complete", "complete_unverified", "reanimate", "abort"}:
                raise ValueError(f"Invalid stop-hook action: {value.action}")
            return value
        if isinstance(value, str):
            return StopHookDecision(action="reanimate", message=value)
        if isinstance(value, dict):
            source = str(value.get("source") or "")
            if value.get("abort") is True:
                return StopHookDecision(
                    action="abort",
                    message=str(value.get("reason") or ""),
                    source=source,
                )
            if "reanimate" in value:
                return StopHookDecision(
                    action="reanimate",
                    message=str(value.get("reanimate") or ""),
                    source=source,
                )
        raise TypeError("Stop hook must return None, str, StopHookDecision, or a result dict")

    def after_tool_result(self, loop: Any, messages: list, result: Any, output: str) -> None:
        ctx = ToolResultContext(loop=loop, messages=messages, result=result, output=output)
        for hook in self.after_tool_result_hooks:
            hook(ctx)

    def after_tool_batch(
        self,
        loop: Any,
        messages: list,
        *,
        manual_compact: bool,
        used_todo: bool,
        on_text: Callable | None,
        write_total: int,
        write_denied: int,
    ) -> None:
        ctx = ToolBatchContext(
            loop=loop,
            messages=messages,
            manual_compact=manual_compact,
            used_todo=used_todo,
            on_text=on_text,
            write_total=write_total,
            write_denied=write_denied,
        )
        for hook in self.after_tool_batch_hooks:
            hook(ctx)

    def on_turn_start(self, loop: Any, messages: list) -> None:
        self._run_configured_event(self._base_context(loop, messages, "turn_start"))

    def on_pre_send(self, loop: Any, messages: list) -> None:
        self._run_configured_event(self._base_context(loop, messages, "pre_send"))

    def on_post_receive(self, loop: Any, messages: list, *, message: str = "") -> None:
        self._run_configured_event(self._base_context(loop, messages, "post_receive", message=message))

    def on_turn_end(self, loop: Any, messages: list, *, status: str = "") -> None:
        self._run_configured_event(self._base_context(loop, messages, "turn_end", status=status))

    def before_tool_use(
        self,
        loop: Any,
        messages: list,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        file_path: str = "",
        is_write: bool = False,
    ) -> HookDecision | None:
        return self._run_configured_event(
            self._base_context(
                loop,
                messages,
                "pre_tool_use",
                tool_name=tool_name,
                tool_args=tool_args,
                file_path=file_path,
                is_write=is_write,
            )
        )

    def on_post_tool_use(
        self,
        loop: Any,
        messages: list,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        file_path: str = "",
        output: str = "",
        status: str = "",
        is_write: bool = False,
    ) -> None:
        self._run_configured_event(
            self._base_context(
                loop,
                messages,
                "post_tool_use",
                tool_name=tool_name,
                tool_args=tool_args,
                file_path=file_path,
                output=output,
                status=status,
                is_write=is_write,
            )
        )

    def _run_configured_event(self, ctx: HookContext) -> HookDecision | None:
        continue_decision: HookDecision | None = None
        for hook in self.configured_hooks:
            if hook.event != ctx.event_name:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            try:
                hook.mark_executed()
                text = self._render_hook_message(ctx, hook)
                if hook.action.type == "prompt" and text:
                    self._prompt_messages.append(text)
                rejected = hook.reject
                continued = hook.continue_run
                self._trace_hook(ctx, hook, text, rejected=rejected, continued=continued)
                if rejected:
                    reason = text or f"Blocked by hook {hook.id}"
                    return HookDecision(rejected=True, message=reason, hook_id=hook.id)
                if continued:
                    continue_decision = HookDecision(continue_run=True, message=text, hook_id=hook.id)
            except Exception as exc:
                decision = self._handle_hook_error(ctx, hook, exc)
                if decision is not None:
                    return decision
        return continue_decision

    def _render_hook_message(self, ctx: HookContext, hook: ConfiguredHook) -> str:
        return ctx.expand(hook.action.message).strip()

    def _handle_hook_error(self, ctx: HookContext, hook: ConfiguredHook, exc: Exception) -> HookDecision | None:
        error_ctx = replace(ctx, error=str(exc))
        policy = hook.on_error
        decision = "continue"
        prompt_text = ""
        if policy == "prompt":
            prompt_text = self._render_error_message(error_ctx, hook, exc)
            if prompt_text:
                self._prompt_messages.append(prompt_text)
        elif policy == "reject":
            decision = "reject"
            reason = self._render_error_message(error_ctx, hook, exc) or f"Hook {hook.id} failed: {exc}"
            self._trace_hook_error(error_ctx, hook, exc, decision=decision, prompt_text=reason)
            return HookDecision(rejected=True, message=reason, hook_id=hook.id)
        self._trace_hook_error(error_ctx, hook, exc, decision=decision, prompt_text=prompt_text)
        if policy == "log":
            log.warning("Configured hook '%s' failed on %s: %s", hook.id, hook.event, exc)
        return None

    def _render_error_message(self, ctx: HookContext, hook: ConfiguredHook, exc: Exception) -> str:
        template = hook.error_message or f"Hook {hook.id} failed on {hook.event}: $ERROR"
        try:
            return ctx.expand(template).strip()
        except Exception:
            return f"Hook {hook.id} failed on {hook.event}: {exc}"

    def _trace_hook(
        self,
        ctx: HookContext,
        hook: ConfiguredHook,
        output: str,
        *,
        rejected: bool,
        continued: bool,
    ) -> None:
        tracer = getattr(ctx.loop, "tracer", None)
        if tracer is None:
            return
        try:
            tracer.log(
                "hook_triggered",
                hook_id=hook.id,
                hook_event=hook.event,
                action=hook.action.type,
                reject=hook.reject,
                continue_run=hook.continue_run,
                decision=("reject" if rejected else ("continue" if continued else "allow")),
                on_error=hook.on_error,
                tool_name=ctx.tool_name,
                file_path=ctx.file_path,
                conflicting_requested_path=ctx.conflicting_requested_path,
                status=ctx.status,
                turn_count=ctx.turn_count,
                message_count=ctx.message_count,
                task_mode=ctx.task_mode,
                wants_tests=ctx.wants_tests,
                tests_modified=ctx.tests_modified,
                missing_requested_paths_count=len(ctx.missing_requested_paths),
                missing_requested_test_paths_count=len(ctx.missing_requested_test_paths),
                is_write=ctx.is_write,
                output=output[:300],
            )
        except Exception:
            return

    def _trace_hook_error(
        self,
        ctx: HookContext,
        hook: ConfiguredHook,
        exc: Exception,
        *,
        decision: str,
        prompt_text: str = "",
    ) -> None:
        tracer = getattr(ctx.loop, "tracer", None)
        if tracer is None:
            return
        try:
            tracer.log(
                "hook_failed",
                hook_id=hook.id,
                hook_event=hook.event,
                action=hook.action.type,
                on_error=hook.on_error,
                continue_run=hook.continue_run,
                decision=decision,
                error=str(exc),
                error_type=type(exc).__name__,
                tool_name=ctx.tool_name,
                file_path=ctx.file_path,
                conflicting_requested_path=ctx.conflicting_requested_path,
                turn_count=ctx.turn_count,
                message_count=ctx.message_count,
                task_mode=ctx.task_mode,
                wants_tests=ctx.wants_tests,
                tests_modified=ctx.tests_modified,
                missing_requested_paths_count=len(ctx.missing_requested_paths),
                missing_requested_test_paths_count=len(ctx.missing_requested_test_paths),
                is_write=ctx.is_write,
                prompt_text=prompt_text[:300],
            )
        except Exception:
            return

    def _base_context(self, loop: Any, messages: list, event_name: str, **kwargs: Any) -> HookContext:
        runtime_state = getattr(loop, "runtime_state", None)
        run_evidence = getattr(loop, "run_evidence", None)

        requested_paths = _unique_normalized_paths(getattr(runtime_state, "requested_paths", []) or [])
        requested_test_paths = [path for path in requested_paths if is_test_file(path)]
        changed_files = _unique_normalized_paths(getattr(runtime_state, "changed_files", []) or [])

        created_files = _unique_normalized_paths(getattr(run_evidence, "created_files", []) or [])
        modified_files = _unique_normalized_paths(getattr(run_evidence, "modified_files", []) or [])
        actual_output_paths = _unique_normalized_paths(getattr(run_evidence, "actual_output_paths", []) or [])
        if not actual_output_paths:
            actual_output_paths = _unique_normalized_paths(created_files + modified_files)
        created_test_files = [path for path in created_files if is_test_file(path)]
        modified_test_files = [path for path in modified_files if is_test_file(path)]
        actual_test_output_paths = [path for path in actual_output_paths if is_test_file(path)]

        missing_requested_paths = [path for path in requested_paths if path not in set(actual_output_paths)]
        missing_requested_test_paths = [path for path in requested_test_paths if path not in set(actual_output_paths)]

        raw_file_path = str(kwargs.get("file_path") or "")
        normalized_file_path = _normalize_path_value(raw_file_path)
        conflicting_requested_path = _conflicting_requested_path(normalized_file_path, requested_paths)
        requested_path_exact_match = bool(normalized_file_path and normalized_file_path in set(requested_paths))
        requested_basename_match = bool(_basename_match(normalized_file_path, requested_paths))

        return HookContext(
            loop=loop,
            messages=messages,
            event_name=event_name,
            session_id=str(getattr(loop, "session_id", "") or ""),
            agent_id=str(getattr(loop, "agent_id", "") or ""),
            trace_id=str(getattr(loop, "trace_id", "") or ""),
            turn_count=int(getattr(runtime_state, "turn_count", 0) or 0),
            message_count=len(messages),
            task_mode=str(getattr(runtime_state, "task_mode", "") or ""),
            initial_task=str(getattr(runtime_state, "initial_task_text", "") or ""),
            last_user_message=_last_message_text(messages, "user"),
            last_assistant_message=_last_message_text(messages, "assistant"),
            acceptance_criteria=list(getattr(runtime_state, "acceptance_criteria", []) or []),
            requested_paths=requested_paths,
            requested_test_paths=requested_test_paths,
            missing_requested_paths=missing_requested_paths,
            missing_requested_test_paths=missing_requested_test_paths,
            created_files=created_files,
            modified_files=modified_files,
            actual_output_paths=actual_output_paths,
            created_test_files=created_test_files,
            modified_test_files=modified_test_files,
            actual_test_output_paths=actual_test_output_paths,
            changed_files=changed_files,
            edits_this_run=int(getattr(runtime_state, "edits_this_run", 0) or 0),
            has_diff=bool(getattr(runtime_state, "has_diff", False)),
            wants_tests=bool(getattr(runtime_state, "wants_tests", False)),
            tests_modified=bool(getattr(runtime_state, "tests_modified", False)),
            normalized_file_path=normalized_file_path,
            file_basename=_basename(normalized_file_path),
            file_dir=_dirname(normalized_file_path),
            first_requested_path=requested_paths[0] if requested_paths else "",
            first_requested_test_path=requested_test_paths[0] if requested_test_paths else "",
            first_missing_requested_path=missing_requested_paths[0] if missing_requested_paths else "",
            first_missing_requested_test_path=(
                missing_requested_test_paths[0] if missing_requested_test_paths else ""
            ),
            conflicting_requested_path=conflicting_requested_path,
            is_test_file=bool(normalized_file_path and is_test_file(normalized_file_path)),
            requested_path_exact_match=requested_path_exact_match,
            requested_basename_match=requested_basename_match,
            same_basename_conflict=bool(conflicting_requested_path),
            **kwargs,
        )


def build_default_hooks() -> AgentHooks:
    # InfCode's SessionPrompt loop finishes as soon as the assistant produces
    # a non-tool-call finish. Verification is model-directed prompt guidance,
    # and reflection is an explicit agent/tool choice; neither is an implicit
    # completion gate. Keep the legacy hooks importable for opt-in consumers,
    # but do not put them on the production Agent loop.
    hooks = AgentHooks(
        before_no_tool_response_hooks=[],
        stop_hooks=[strict_generation_stop_hook],
        after_tool_result_hooks=[tool_failure_diagnostic_hook],
        after_tool_batch_hooks=[
            all_writes_denied_hook,
            todo_reminder_hook,
            manual_compact_hook,
        ],
    )
    for hook in load_configured_hooks_from_settings():
        hooks.register_configured_hook(hook)
    return hooks


def strict_generation_stop_hook(context: StopHookContext) -> StopHookDecision:
    """Require settled final-generation evidence only in strict SWE mode.

    This is the production consumer for the InfCodeX-style bounded stop-hook
    contract. The policy itself is an NZ-Coder SWE adapter and is inert for
    ordinary terminal, HTTP, and child Agent runs.
    """
    from nz_coder.runtime.core.execution_context import strict_local_tools

    if not strict_local_tools():
        return StopHookDecision()
    state = context.runtime_state
    generation = int(state.get("mutation_generation", 0) or 0)
    if generation <= 0:
        return StopHookDecision()
    verification = state.get("verification") or {}
    verification_needed = bool(verification.get("verification_needed"))
    if state.get("strict_generation_ready") is True and not verification_needed:
        return StopHookDecision()
    environment_blocker = verification.get("environment_blocker") or {}
    blocker_stage = str(environment_blocker.get("stage") or "")
    if (
        verification.get("verification_state") == "blocked_environment"
        and blocker_stage == "targeted"
        and state.get("diff_generation") == generation
        and state.get("has_diff") is True
    ):
        from nz_coder.runtime.verification.recovery import repository_test_runner_recovery_command

        last_verification = verification.get("last_verification") or {}
        recovery_command = repository_test_runner_recovery_command(
            "bash",
            str(
                environment_blocker.get("output")
                or last_verification.get("output")
                or ""
            ),
            tool_input={
                "command": str(
                    environment_blocker.get("command")
                    or last_verification.get("command")
                    or ""
                ),
            },
        )
        if recovery_command:
            return StopHookDecision(
                action="reanimate",
                message=(
                    "The targeted check is recoverable from repository evidence. "
                    f"Run this exact workspace-local retry now: `{recovery_command}`. "
                    "Do not finish or change source code until this retry settles."
                ),
                source="strict-generation-verifier",
            )
        return StopHookDecision(
            action="complete_unverified",
            message="Verification infrastructure is unavailable for this generation.",
            source="strict-generation-verifier",
        )
    pipeline = verification.get("verification_pipeline") or {}
    stages = pipeline.get("stages") or []
    targeted_evidence_pending = any(
        stage.get("name") == "targeted"
        and stage.get("evidence_required") is True
        and stage.get("status") != "passed"
        for stage in stages
    )
    pending = []
    for stage in stages:
        for item in stage.get("commands") or []:
            if item.get("required") and item.get("status") != "passed":
                command = str(item.get("command") or "").strip()
                if command and command not in pending:
                    pending.append(command)
    pending_hint = (
        " Pending required evidence: " + "; ".join(pending[:3]) + "."
        if pending else ""
    )
    targeted_hint = (
        " Run one direct narrow behavioral test that exercises the changed "
        "behavior, and confirm that it executes at least one test."
        if targeted_evidence_pending else ""
    )
    return StopHookDecision(
        action="reanimate",
        message=(
            "The current source mutation generation has not settled. Call "
            "diff_status, then verify_changed_files for that same generation. "
            "If a targeted check failed, repair the code and rerun that check. "
            "If verification cannot run, finish with a concrete blocker."
            + targeted_hint
            + pending_hint
        ),
        source="strict-generation-verifier",
    )


def verification_gate_hook(ctx: NoToolResponseContext) -> str:
    loop = ctx.loop
    messages = ctx.messages
    if loop.vm.should_gate() and _is_keep_going(messages):
        loop.vm.reset()
        loop.tracer.log("verification_gate_bypassed", reason="keep_going")

    if not loop.vm.should_gate():
        return "completed"

    if loop.vm.increment_gate_prompt() <= config.MAX_VERIFICATION_GATE_PROMPTS:
        messages.append(stamp_user_message({
            "role": "user",
            "content": loop.vm.make_gate_message(),
            "_nz_synthetic": True,
        }))
        loop.tracer.log(
            "verification_gate",
            prompts=loop.vm.gate_prompts,
            **loop.vm.status(),
        )
        return "continue"
    return "completed_unverified"


def reflection_gate_hook(ctx: NoToolResponseContext) -> str:
    loop = ctx.loop
    return loop._check_reflection_gate(ctx.messages, ctx.status, ctx.message)


def _tool_failure_diagnostic_skip_reason(result: Any, output: str) -> str:
    """Return why an already-actionable policy failure needs no second prompt."""
    if "doom loop detected" in str(output or "").casefold():
        # Recovery adds a specific change-of-approach contract for this case;
        # it is materially stronger than the short admission result.
        return ""
    metadata = getattr(result, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    if (
        bool(getattr(result, "permission_denied", False))
        or str(output or "").startswith("Denied")
        or any(
            key in metadata
            for key in (
                "guardrail",
                "invariant",
                "stall_nudge",
            )
        )
    ):
        return "policy_owned_rejection"

    text = str(output or "")
    actionable_policy_markers = (
        "in SWE-bench strict mode",
        "Error: sed -i is blocked.",
        "Error: Dangerous command blocked",
        "Error: Package install blocked.",
        "Error: Broad test runner blocked.",
        "Error: Read-only shell blocked",
        "Error: Verification pipelines require Bash pipefail",
    )
    if any(marker in text for marker in actionable_policy_markers):
        return "actionable_policy_output"
    return ""


def tool_failure_diagnostic_hook(ctx: ToolResultContext) -> None:
    runtime_state = getattr(ctx.loop, "runtime_state", None)
    tool_input = getattr(ctx.result, "tool_input", None)
    diagnostic_output = str(ctx.output or "")
    if ctx.result.name == "bash" and isinstance(tool_input, dict):
        vm = getattr(ctx.loop, "vm", None)
        status_reader = getattr(vm, "status", None)
        try:
            verification = status_reader() if callable(status_reader) else {}
        except Exception:
            verification = {}
        blocker = (
            verification.get("environment_blocker") or {}
            if isinstance(verification, dict)
            else {}
        )
        command = " ".join(str(tool_input.get("command") or "").split())
        blocked_command = " ".join(str(blocker.get("command") or "").split())
        if (
            verification.get("verification_state") == "blocked_environment"
            and command
            and command == blocked_command
        ):
            from nz_coder.runtime.verification.recovery import repository_test_runner_recovery_command

            recovery_command = ""
            for candidate_output in dict.fromkeys((
                diagnostic_output,
                str(blocker.get("output") or ""),
            )):
                recovery_command = repository_test_runner_recovery_command(
                    ctx.result.name,
                    candidate_output,
                    tool_input=tool_input,
                )
                if recovery_command:
                    diagnostic_output = candidate_output
                    break
            if not recovery_command:
                ctx.loop.tracer.log(
                    "tool_failure_diagnostic_skipped",
                    name=ctx.result.name,
                    reason="verification_environment_blocker",
                )
                return
    skip_reason = _tool_failure_diagnostic_skip_reason(ctx.result, ctx.output)
    if skip_reason:
        ctx.loop.tracer.log(
            "tool_failure_diagnostic_skipped",
            name=ctx.result.name,
            reason=skip_reason,
        )
        return
    contract = getattr(runtime_state, "task_contract", {})
    declared_paths = tuple(dict.fromkeys(
        str(path)
        for requirement in contract.get("requirements", ())
        if isinstance(requirement, dict)
        for path in requirement.get("expected_artifacts", ())
        if str(path).strip()
    )) if isinstance(contract, dict) else ()
    kwargs = {}
    if isinstance(tool_input, dict):
        kwargs["tool_input"] = tool_input
    if declared_paths:
        kwargs["declared_paths"] = declared_paths
    diagnostic = ctx.loop.recovery.tool_failure_diagnostic(
        ctx.result.name,
        diagnostic_output,
        **kwargs,
    )
    if not diagnostic:
        return
    recorder = getattr(runtime_state, "record_recovery_diagnostic", None)
    if callable(recorder):
        recorder(diagnostic)
    if _last_user_has_frustration(ctx.messages):
        diagnostic = (
            "<user-frustration-context>\n"
            "The user seems frustrated. Acknowledge the difficulty briefly, "
            "then focus immediately on the concrete fix below.\n"
            "</user-frustration-context>\n" + diagnostic
        )
    ctx.messages.append(stamp_user_message({
        "role": "user",
        "content": diagnostic,
        "_nz_synthetic": True,
    }))
    ctx.loop.tracer.log(
        "tool_failure_diagnostic",
        name=ctx.result.name,
        primary=getattr(runtime_state, "primary_recovery_classification", ""),
        supporting=list(getattr(
            runtime_state,
            "supporting_recovery_classifications",
            [],
        )),
        repair_targets=list(getattr(runtime_state, "recovery_repair_targets", [])),
    )


def all_writes_denied_hook(ctx: ToolBatchContext) -> None:
    if ctx.write_total <= 0 or ctx.write_denied != ctx.write_total:
        return
    ctx.messages.append(stamp_user_message({
        "role": "user",
        "content": "所有写操作均被用户拒绝，请停止修改文件，向用户说明情况。",
        "_nz_synthetic": True,
    }))


def todo_reminder_hook(ctx: ToolBatchContext) -> None:
    runtime_state = getattr(ctx.loop, "runtime_state", None)
    contract_owns_progress = getattr(
        runtime_state,
        "contract_owns_progress",
        lambda: False,
    )
    if contract_owns_progress():
        ctx.loop.rounds_without_todo = 0
        return
    ctx.loop.rounds_without_todo = 0 if ctx.used_todo else ctx.loop.rounds_without_todo + 1
    reminder = get_reminder(ctx.loop.rounds_without_todo)
    if reminder:
        ctx.messages.append(stamp_user_message({
            "role": "user",
            "content": reminder,
            "_nz_synthetic": True,
        }))


def manual_compact_hook(ctx: ToolBatchContext) -> None:
    if not ctx.manual_compact:
        return
    if ctx.on_text:
        ctx.on_text("[manual compact]")
    ctx.loop.tracer.log("compact", kind="manual")
    ctx.messages[:] = ctx.loop._compact_messages(ctx.messages)
    ctx.loop._on_context_compacted()


def load_configured_hooks_from_settings(
    settings_path: Path | None = None,
    *,
    strict: bool = False,
) -> list[ConfiguredHook]:
    path = settings_path or (current_workdir() / ".nz-coder" / "settings.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise HookConfigError(f"Failed to load hook settings from {path}: {exc}") from exc
        log.warning("Failed to load hook settings from %s: %s", path, exc)
        return []
    raw_hooks = data.get("hooks", [])
    if not raw_hooks:
        return []
    if not isinstance(raw_hooks, list):
        if strict:
            raise HookConfigError(f"Hook settings at {path}: 'hooks' must be a list")
        log.warning("Ignoring hook settings at %s: 'hooks' must be a list", path)
        return []
    try:
        return parse_configured_hooks(raw_hooks)
    except HookConfigError as exc:
        if strict:
            raise
        log.warning("Ignoring invalid hook settings at %s: %s", path, exc)
        return []


def parse_configured_hooks(raw_hooks: list[dict]) -> list[ConfiguredHook]:
    hooks: list[ConfiguredHook] = []
    for index, entry in enumerate(raw_hooks):
        label = _identify_hook(entry, index)
        if not isinstance(entry, dict):
            raise HookConfigError(f"{label}: must be an object")
        event = str(entry.get("event") or "").strip()
        if event not in _VALID_CONFIG_HOOK_EVENTS:
            choices = ", ".join(sorted(_VALID_CONFIG_HOOK_EVENTS))
            raise HookConfigError(f"{label}: invalid event '{event}', expected one of: {choices}")
        raw_action = entry.get("action")
        if not isinstance(raw_action, dict):
            raise HookConfigError(f"{label}: missing or invalid 'action'")
        action_type = str(raw_action.get("type") or "").strip()
        if action_type not in _VALID_ACTION_TYPES:
            choices = ", ".join(sorted(_VALID_ACTION_TYPES))
            raise HookConfigError(f"{label}: invalid action type '{action_type}', expected one of: {choices}")
        message = str(raw_action.get("message") or "").strip()
        if action_type == "prompt" and not message:
            raise HookConfigError(f"{label}: prompt action requires non-empty 'message'")
        reject = bool(entry.get("reject", False))
        if reject and event != "pre_tool_use":
            raise HookConfigError(f"{label}: reject=true is only allowed for pre_tool_use")
        continue_run = bool(entry.get("continue", False))
        if continue_run and event != "no_tool_response":
            raise HookConfigError(f"{label}: continue=true is only allowed for no_tool_response")
        if continue_run and reject:
            raise HookConfigError(f"{label}: reject=true and continue=true cannot be used together")
        on_error = str(entry.get("on_error") or "log").strip()
        if on_error not in _VALID_HOOK_ERROR_POLICIES:
            choices = ", ".join(sorted(_VALID_HOOK_ERROR_POLICIES))
            raise HookConfigError(f"{label}: invalid on_error '{on_error}', expected one of: {choices}")
        if on_error == "reject" and event != "pre_tool_use":
            raise HookConfigError(f"{label}: on_error=reject is only allowed for pre_tool_use")
        error_message = str(entry.get("error_message") or "").strip()
        condition = None
        raw_if = entry.get("if")
        if raw_if:
            try:
                condition = parse_hook_condition(str(raw_if))
            except HookConditionParseError as exc:
                raise HookConfigError(f"{label}: invalid condition: {exc}") from exc
        hooks.append(
            ConfiguredHook(
                id=str(entry.get("id") or f"{event}_{index}"),
                event=event,
                action=HookAction(type=action_type, message=message),
                condition=condition,
                reject=reject,
                continue_run=continue_run,
                on_error=on_error,
                error_message=error_message,
                once=bool(entry.get("once", False)),
            )
        )
    return hooks


def parse_hook_condition(expr: str) -> HookConditionGroup | None:
    if not expr or not expr.strip():
        return None
    expr = expr.strip()
    has_and = "&&" in expr
    has_or = "||" in expr
    if has_and and has_or:
        raise HookConditionParseError("Cannot mix '&&' and '||' in a single hook condition")
    if has_and:
        parts = expr.split("&&")
        logic = "and"
    elif has_or:
        parts = expr.split("||")
        logic = "or"
    else:
        parts = [expr]
        logic = "and"
    conditions = [_parse_single_condition(part) for part in parts]
    return HookConditionGroup(conditions=conditions, logic=logic)


def _parse_single_condition(expr: str) -> HookCondition:
    text = expr.strip()
    for operator in _OPERATORS:
        index = text.find(operator)
        if index == -1:
            continue
        field_name = text[:index].strip()
        value = text[index + len(operator):].strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return HookCondition(field=field_name, operator=operator, value=value)
    raise HookConditionParseError(f"No valid operator found in condition: '{expr.strip()}'")


def _identify_hook(entry: Any, index: int) -> str:
    if isinstance(entry, dict) and entry.get("id"):
        return f"hook '{entry['id']}'"
    return f"hook #{index + 1}"


def _is_keep_going(messages: list) -> bool:
    for msg in reversed(messages):
        if (
            msg.get("role") == "user"
            and not is_synthetic_user_message(msg)
            and isinstance(msg.get("content"), str)
        ):
            text = msg["content"].strip()
            return bool(_KEEP_GOING_RE.match(text))
    return False


def _last_user_has_frustration(messages: list) -> bool:
    for msg in reversed(messages):
        if (
            msg.get("role") == "user"
            and not is_synthetic_user_message(msg)
            and isinstance(msg.get("content"), str)
        ):
            return bool(_NEGATIVE_RE.search(msg["content"]))
    return False


def _last_message_text(messages: list, role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role and isinstance(msg.get("content"), str):
            return msg["content"][:300]
    return ""


def _normalize_path_value(path: str) -> str:
    return _policy_normalize_path(str(path or "")).strip().lstrip("./")


def _unique_normalized_paths(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        normalized = _normalize_path_value(str(value))
        if normalized and normalized not in items:
            items.append(normalized)
    return items


def _basename(path: str) -> str:
    normalized = _normalize_path_value(path)
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def _dirname(path: str) -> str:
    normalized = _normalize_path_value(path)
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def _basename_match(path: str, requested_paths: list[str]) -> bool:
    base = _basename(path)
    if not base:
        return False
    return any(_basename(item) == base for item in requested_paths)


def _conflicting_requested_path(path: str, requested_paths: list[str]) -> str:
    normalized = _normalize_path_value(path)
    base = _basename(normalized)
    if not normalized or not base:
        return ""
    if normalized in set(requested_paths):
        return ""
    for requested in requested_paths:
        if _basename(requested) == base:
            return requested
    return ""
