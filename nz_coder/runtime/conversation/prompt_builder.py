"""Model-facing prompt layer assembly for the coding runtime."""
from __future__ import annotations

from nz_coder.foundation import config
from nz_coder.state.context import estimate_tokens
from nz_coder.runtime.conversation.continuation_context import continuation_task_text
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.conversation.structured_output import STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.instructions import load_instruction_context
from nz_coder.tool_platform.exposure import current_exposure_plan, expose_specs


class ProductionPromptBuilder:
    """Assemble stable, dynamic and instruction layers for one model request."""

    def build(self, host, messages: list) -> list:
        if getattr(host, "_structured_output_active_repair", ""):
            sanitized = host._sanitize_messages(messages)
            host.tracer.log(
                "context_layers",
                stable_tokens=estimate_text_tokens(
                    STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
                ),
                dynamic_tokens=0,
                scratch_tokens=0,
                repair=True,
            )
            return [{
                "role": "system",
                "content": STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT,
            }, *sanitized]
        if getattr(host, "runtime_profile", "") in {"read_child", "write_child"}:
            sanitized = host._sanitize_messages(messages)
            host.tracer.log(
                "context_layers",
                stable_tokens=estimate_text_tokens(host.system_prompt),
                dynamic_tokens=0,
                scratch_tokens=0,
                child_profile=True,
            )
            return [{"role": "system", "content": host.system_prompt}, *sanitized]

        task_query = _task_query(host, messages)
        scratch = host._sp.build_prompt_block()
        memory = host._memory_block(task_query)
        progress_nudges = host.runtime_state.strict_progress_nudges
        state = host.runtime_state.build_prompt_block(strict=strict_local_tools())
        if host.runtime_state.strict_progress_nudges != progress_nudges:
            host.tracer.log(
                "strict_progress_nudge",
                investigation_calls=host.runtime_state.investigation_calls_since_edit,
                mutation_generation=host.runtime_state.mutation_generation,
            )
        dynamic_state = "\n".join(
            part for part in (
                host._project_profile_block(),
                state,
                host._hook_prompt_block(),
                host._lineage_recovery_block(messages),
                host._implementation_bundle_block(task_query),
                host._repo_retrieval_block(task_query),
            ) if part
        )
        active_system = host.system_prompt
        plan = host._plan_mode_prompt_block()
        if plan:
            active_system += "\n\n" + plan
        stable_system, dynamic_context, stats = build_context_layers(
            active_system,
            memory,
            dynamic_state,
            scratch,
            max_tokens=config.SYSTEM_CONTEXT_BUDGET_TOKENS,
        )
        run_snapshot = getattr(host, "config_snapshot", None)
        instructions = (
            load_instruction_context(
                current_workdir(), config_snapshot=run_snapshot,
            )
            if run_snapshot is not None
            else load_instruction_context(current_workdir())
        )
        sanitized = host._sanitize_messages(messages)
        has_user = any(
            message.get("role") == "user"
            for message in sanitized
            if isinstance(message, dict)
        )
        sanitized = inject_dynamic_context(sanitized, dynamic_context)
        if instructions.reminder:
            if has_user:
                sanitized = inject_instruction_reminder(
                    sanitized,
                    instructions.reminder,
                )
            else:
                stable_system = "\n\n".join(
                    part for part in (stable_system, instructions.reminder) if part
                )
        api_messages = [{"role": "system", "content": stable_system}, *sanitized]
        host.tracer.log("context_layers", **stats)
        host.tracer.log(
            "instruction_context",
            source_count=instructions.source_count,
            included_count=instructions.included_count,
            truncated_count=instructions.truncated_count,
            per_file_truncated_count=instructions.per_file_truncated_count,
            total_truncated_count=instructions.total_truncated_count,
            omitted_count=instructions.omitted_count,
            included_bytes=instructions.included_bytes,
            paths=list(instructions.paths),
            disabled_count=instructions.disabled_count,
            warnings=list(instructions.warnings),
        )
        message_tokens = estimate_tokens(api_messages)
        visible_tools = expose_specs(host._active_tool_specs())
        tool_tokens = estimate_tokens(visible_tools)
        exposure_plan = current_exposure_plan()
        if exposure_plan is not None:
            host.tracer.log(
                "tool_exposure_planned",
                visible_names=list(exposure_plan.visible_names),
                deferred_names=list(exposure_plan.deferred_names),
                hidden_names=list(exposure_plan.hidden_names),
                visible_count=len(exposure_plan.visible_names),
                deferred_count=len(exposure_plan.deferred_names),
                hidden_count=len(exposure_plan.hidden_names),
                estimated_tokens_before=exposure_plan.estimated_tokens_before,
                estimated_tokens_after=exposure_plan.estimated_tokens_after,
                estimated_tokens_saved=max(
                    0,
                    exposure_plan.estimated_tokens_before
                    - exposure_plan.estimated_tokens_after,
                ),
            )
        host.tracer.log(
            "llm_request",
            message_count=len(api_messages),
            message_token_estimate=message_tokens,
            tool_count=len(visible_tools),
            tool_schema_token_estimate=tool_tokens,
            token_estimate=message_tokens + tool_tokens,
        )
        return api_messages


def build_context_layers(
    system_prompt: str,
    memory_block: str,
    state_block: str,
    scratch_block: str,
    max_tokens: int,
) -> tuple[str, str, dict]:
    memory = memory_block or ""
    state = state_block or ""
    scratch = scratch_block or ""
    before = {
        "fixed_tokens": estimate_text_tokens(system_prompt),
        "memory_tokens": estimate_text_tokens(memory),
        "state_tokens": estimate_text_tokens(state),
        "scratch_tokens": estimate_text_tokens(scratch),
    }
    max_tokens = max(1000, int(max_tokens or 6000))
    if sum(before.values()) > max_tokens:
        if estimate_text_tokens(scratch) > 1000:
            scratch = truncate_text_tokens(scratch, 1000, keep_tail=True)
        used = sum(estimate_text_tokens(item) for item in (system_prompt, state, scratch))
        memory = truncate_text_tokens(memory, max(0, max_tokens - used))
        used = sum(estimate_text_tokens(item) for item in (system_prompt, state, memory))
        scratch = truncate_text_tokens(
            scratch,
            max(0, max_tokens - used),
            keep_tail=True,
        )
        used = sum(estimate_text_tokens(item) for item in (system_prompt, memory, scratch))
        state = truncate_text_tokens(state, max(0, max_tokens - used))
    dynamic_parts = [part for part in (memory, state, scratch) if part]
    dynamic_context = ""
    if dynamic_parts:
        dynamic_context = (
            "<context-injection>\n"
            "Recalled background memory, runtime state, and working memory for this turn. "
            "Treat as context, not as a new user request.\n"
            + "\n".join(dynamic_parts)
            + "\n</context-injection>"
        )
    after = {
        "fixed_tokens": estimate_text_tokens(system_prompt),
        "memory_tokens": estimate_text_tokens(memory),
        "state_tokens": estimate_text_tokens(state),
        "scratch_tokens": estimate_text_tokens(scratch),
    }
    return system_prompt, dynamic_context, {
        "budget_tokens": max_tokens,
        "before_total_tokens": sum(before.values()),
        "after_total_tokens": sum(after.values()),
        **{f"before_{key}": value for key, value in before.items()},
        **{f"after_{key}": value for key, value in after.items()},
    }


def inject_dynamic_context(messages: list[dict], dynamic_context: str) -> list[dict]:
    if not dynamic_context:
        return messages
    injected = [dict(message) for message in messages]
    for index, message in enumerate(injected):
        if message.get("role") == "user" and isinstance(message.get("content", ""), str):
            injected[index] = dict(
                message,
                content=dynamic_context + "\n\n" + message.get("content", ""),
            )
            return injected
    return [{"role": "user", "content": dynamic_context}, *injected]


def inject_instruction_reminder(messages: list[dict], reminder: str) -> list[dict]:
    if not reminder:
        return messages
    injected = [dict(message) for message in messages]
    for index, message in enumerate(injected):
        if message.get("role") == "user" and isinstance(message.get("content", ""), str):
            injected[index] = dict(
                message,
                content=reminder + "\n\n" + message.get("content", ""),
            )
            return injected
    return injected


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for character in text if ord(character) < 128)
    return ascii_chars // 4 + len(text) - ascii_chars


def truncate_text_tokens(text: str, max_tokens: int, keep_tail: bool = False) -> str:
    if not text or max_tokens <= 0:
        return ""
    if estimate_text_tokens(text) <= max_tokens:
        return text
    marker = "\n[... truncated by context budget ...]\n"
    payload_tokens = max(0, max_tokens - estimate_text_tokens(marker))
    if payload_tokens <= 0:
        return marker if max_tokens >= estimate_text_tokens(marker) else ""
    max_chars = max(1, payload_tokens * 4)
    return marker + text[-max_chars:] if keep_tail else text[:max_chars] + marker


def _task_query(host, messages: list) -> str:
    """Return one authoritative query for all task-aware prompt layers."""
    query = continuation_task_text(
        messages,
        canonical_task=getattr(host.runtime_state, "initial_task_text", ""),
    )
    if not query:
        query = getattr(host.runtime_state, "initial_task_text", "")
    return query[:300] if isinstance(query, str) else ""
