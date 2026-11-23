"""Context-local runtime overrides for concurrent agent execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import re

from nz_coder.foundation import config
from nz_coder.runtime.core.run_settings import active_run_settings


@dataclass(frozen=True)
class CommandActivation:
    """Canonical activation facts shared by parent preview and child lifecycle."""

    task_text: str
    resume_activation: bool
    pure_continuation: bool
    current_user_message: dict | None


@dataclass(frozen=True)
class CommandExecutionPolicy:
    """The initialized policy; fingerprint always names its original baseline."""

    strict_local_tools: bool
    fingerprint: str


@dataclass(frozen=True)
class CommandPolicyRestriction:
    """None保持原值；strict限制只能收紧。"""

    strict_local_tools: bool | None = None


def is_genuine_runtime_followup(message: dict | None) -> bool:
    from nz_coder.protocol.message_schema import COMPACTION_KEY, CONTINUATION_KEY, is_synthetic_user_message

    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and not is_synthetic_user_message(message)
        and isinstance(message.get("content"), str)
        and COMPACTION_KEY not in message
        and CONTINUATION_KEY not in message
        and not message["content"].lstrip().lower().startswith(("<session-summary>", "<continuation-context>"))
    )


_NEW_TASK_DECLARATION = re.compile(
    r"(?:^[ \t]*|(?<=[.!?])[ \t]+|(?<=[。！？])[ \t]*)"
    r"(?:new (?:independent )?task|新的独立任务|新任务)[ \t]*[:：]",
    re.IGNORECASE | re.MULTILINE,
)


def explicit_replacement_task(message: dict | None) -> str | None:
    """只识别真实用户的直接新任务声明，不推断一般自然语言意图。"""
    if not is_genuine_runtime_followup(message):
        return None
    text = message["content"]
    view = _unquoted_task_declarations(text)
    if view is None:
        return None
    declarations = []
    for match in _NEW_TASK_DECLARATION.finditer(view):
        # 逗号/冒号后的换行仍属于前一句条件或示例，不是独立声明。
        prefix = view[:match.start()].rstrip()
        if prefix and prefix[-1] in ",，:：\\":
            continue
        declarations.append(match)
    if len(declarations) != 1:
        return None
    return text[declarations[0].end():].strip() or None


def _unquoted_task_declarations(text: str) -> str | None:
    """保留字符位置，遮住 Markdown 代码/引用；不确定的引号不授予替换权。"""
    view = list(text)
    fence = ""
    inline = ""
    quote = ""
    offset = 0
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            excluded = True
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
        elif not inline and not quote and marker:
            fence = marker[1]
            excluded = True
        else:
            excluded = (not inline and not quote and
                        (stripped.startswith(">") or line.startswith(("    ", "\t"))))
        if excluded:
            for index, char in enumerate(line):
                if char not in "\r\n":
                    view[offset + index] = " "
            offset += len(line)
            continue
        index = 0
        while index < len(line):
            char = line[index]
            absolute = offset + index
            if char == "`" and not quote:
                end = index + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                run = line[index:end]
                if not inline:
                    inline = run
                elif inline == run:
                    inline = ""
                view[absolute:offset + end] = " " * len(run)
                index = end
                continue
            if inline:
                if char not in "\r\n":
                    view[absolute] = " "
            elif quote:
                if char == quote and (absolute == 0 or text[absolute - 1] != "\\"):
                    quote = ""
                if char not in "\r\n":
                    view[absolute] = " "
            elif char in pairs:
                apostrophe = (char == "'" and absolute > 0 and absolute + 1 < len(text)
                              and text[absolute - 1].isalnum() and text[absolute + 1].isalnum())
                if not apostrophe:
                    quote = pairs[char]
                    view[absolute] = " "
            index += 1
        offset += len(line)
    return None if quote or inline or fence else "".join(view)


def derive_command_execution_policy(
    activation: CommandActivation, *, restored: bool, initial_task_text: str,
    strict_local_tools: bool,
) -> CommandExecutionPolicy:
    task = (
        initial_task_text
        if restored and activation.pure_continuation and initial_task_text
        else activation.task_text
    )
    facts = {
        "schema": "nz.command.v1", "strict": strict_local_tools,
        "resume": activation.resume_activation,
        "pure": activation.pure_continuation, "restored": restored,
        "followup": is_genuine_runtime_followup(activation.current_user_message),
        "activation_task": hashlib.sha256(activation.task_text.encode("utf-8")).hexdigest(),
        "policy_task": hashlib.sha256(task.encode("utf-8")).hexdigest(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    return CommandExecutionPolicy(strict_local_tools, fingerprint)


def tighten_command_policy(
    policy: CommandExecutionPolicy, restriction: CommandPolicyRestriction,
) -> CommandExecutionPolicy:
    return replace(
        policy,
        strict_local_tools=policy.strict_local_tools or restriction.strict_local_tools is True,
    )


def capture_command_restriction() -> CommandPolicyRestriction:
    return CommandPolicyRestriction(strict_local_tools())


@dataclass(frozen=True)
class RuntimeOverrides:
    """Per-execution settings that may differ between concurrent agents."""

    max_agent_turns: int | None = None
    nominal_agent_turns: int | None = None
    agent_timeout_seconds: float | None = None
    max_parallel_tasks: int | None = None
    strict_local_tools: bool | None = None
    repo_intelligence_mode: str | None = None
    repo_retrieval_strategy: str | None = None


_RUNTIME_OVERRIDES: ContextVar[RuntimeOverrides] = ContextVar(
    "nz_coder_runtime_overrides",
    default=RuntimeOverrides(),
)


def max_agent_turns() -> int:
    """Return the current execution's agent-turn limit."""
    value = _RUNTIME_OVERRIDES.get().max_agent_turns
    settings = active_run_settings()
    default = settings.max_agent_turns if settings is not None else config.MAX_AGENT_TURNS
    return max(1, int(default if value is None else value))


def nominal_agent_turns() -> int:
    """Return the convergence SLA without exceeding the execution hard cap."""
    value = _RUNTIME_OVERRIDES.get().nominal_agent_turns
    if value is None:
        settings = active_run_settings()
        value = (
            settings.nominal_agent_turns
            if settings is not None else getattr(config, "NOMINAL_AGENT_TURNS", 15)
        )
    return min(max_agent_turns(), max(1, int(value)))


def agent_timeout_seconds() -> float:
    """Return the current execution's total agent timeout, or zero if disabled."""
    value = _RUNTIME_OVERRIDES.get().agent_timeout_seconds
    if value is None:
        value = getattr(config, "AGENT_TIMEOUT_SECONDS", 0)
    return max(0.0, float(value or 0))


def max_parallel_tasks() -> int:
    """Return the current execution's scheduler concurrency limit."""
    value = _RUNTIME_OVERRIDES.get().max_parallel_tasks
    settings = active_run_settings()
    default = settings.max_parallel_tasks if settings is not None else config.MAX_PARALLEL_TASKS
    return max(1, int(default if value is None else value))


def strict_local_tools() -> bool:
    """Return whether this execution forbids network-capable tool behavior."""
    return bool(_RUNTIME_OVERRIDES.get().strict_local_tools)


def repo_intelligence_mode() -> str:
    """Return the benchmark/runtime retrieval tier for repository tools."""
    return str(_RUNTIME_OVERRIDES.get().repo_intelligence_mode or "lookup")


def repo_retrieval_strategy() -> str:
    """Resolve an explicit scoped strategy, otherwise use bounded task routing."""
    return str(_RUNTIME_OVERRIDES.get().repo_retrieval_strategy or "policy")


@contextmanager
def scoped_runtime_overrides(
    *,
    max_agent_turns: int | None = None,
    nominal_agent_turns: int | None = None,
    agent_timeout_seconds: float | None = None,
    max_parallel_tasks: int | None = None,
    strict_local_tools: bool | None = None,
    repo_intelligence_mode: str | None = None,
    repo_retrieval_strategy: str | None = None,
):
    """Temporarily override runtime settings without mutating global config."""
    current = _RUNTIME_OVERRIDES.get()
    updated = replace(
        current,
        max_agent_turns=(
            current.max_agent_turns
            if max_agent_turns is None
            else max(1, int(max_agent_turns))
        ),
        nominal_agent_turns=(
            current.nominal_agent_turns
            if nominal_agent_turns is None
            else max(1, int(nominal_agent_turns))
        ),
        agent_timeout_seconds=(
            current.agent_timeout_seconds
            if agent_timeout_seconds is None
            else max(0.0, float(agent_timeout_seconds))
        ),
        max_parallel_tasks=(
            current.max_parallel_tasks
            if max_parallel_tasks is None
            else max(1, int(max_parallel_tasks))
        ),
        strict_local_tools=(
            current.strict_local_tools
            if strict_local_tools is None
            else bool(strict_local_tools)
        ),
        repo_intelligence_mode=(
            current.repo_intelligence_mode
            if repo_intelligence_mode is None
            else str(repo_intelligence_mode)
        ),
        repo_retrieval_strategy=(
            current.repo_retrieval_strategy
            if repo_retrieval_strategy is None
            else str(repo_retrieval_strategy)
        ),
    )
    token = _RUNTIME_OVERRIDES.set(updated)
    try:
        yield updated
    finally:
        _RUNTIME_OVERRIDES.reset(token)



def command_activation(messages: list[dict]) -> CommandActivation:
    """父方预演和实际 initialize 使用同一修复后历史，不写入原消息。"""
    import copy
    from nz_coder.protocol.message_schema import cleanup_incomplete_tool_history, is_user_request_message, settle_interrupted_parts
    from nz_coder.runtime.conversation.continuation_context import (
        continuation_task_text, is_continuation_activation, is_pure_continuation_activation,
    )
    from nz_coder.state.input_expansion import INPUT_EXPANSIONS_KEY, USER_TEXT_KEY, user_instruction_text

    normalized = copy.deepcopy(messages)
    settle_interrupted_parts(normalized)
    normalized[:] = cleanup_incomplete_tool_history(normalized)
    # 激活视图只解释用户指令。附件仍原样保留在会话中供模型阅读。
    for message in normalized:
        if (isinstance(message, dict) and is_user_request_message(message)
                and (USER_TEXT_KEY in message or message.get(INPUT_EXPANSIONS_KEY))):
            message["content"] = user_instruction_text(message) or ""
    latest_index = next((index for index in range(len(normalized) - 1, -1, -1)
                         if is_genuine_runtime_followup(normalized[index])), None)
    latest = normalized[latest_index] if latest_index is not None else None
    # 已被 assistant 消费过的用户消息不能因历史重放再次授予新一轮额度或声明。
    if latest_index is not None and any(
        isinstance(m, dict) and m.get("role") == "assistant"
        for m in normalized[latest_index + 1:]
    ):
        latest = None
    replacement = explicit_replacement_task(latest)
    if replacement is not None:
        return CommandActivation(replacement, False, False, latest)
    pure = is_pure_continuation_activation(normalized)
    followup = None
    if not pure and latest is not None:
        if any(isinstance(m, dict) and m.get("role") == "assistant" for m in normalized[:latest_index]):
            followup = latest
    return CommandActivation(
        continuation_task_text(normalized), latest is not None and is_continuation_activation(normalized), pure, followup,
    )
