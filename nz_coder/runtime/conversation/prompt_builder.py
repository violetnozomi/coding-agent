"""Model-facing prompt layer assembly for the coding runtime."""
from __future__ import annotations

import copy
from dataclasses import replace

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.state.context import estimate_tokens
from nz_coder.runtime.conversation.continuation_context import (
    continuation_task_text, build_current_facts, has_authoritative_task,
)
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.conversation.structured_output import STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.instructions import load_instruction_context
from nz_coder.tool_platform.exposure import current_exposure_plan, expose_specs
from nz_coder.runtime.conversation.request_snapshot import (
    FrozenRequestSnapshot,
    message_version,
)
from nz_coder.runtime.agent.planning_runtime import render_execution_handoff
from nz_coder.runtime.agent.task_intent import render_task_assessment
from nz_coder.runtime.agent.planning_runtime import render_working_plan_revision


class ProductionPromptBuilder:
    """Assemble stable, dynamic and instruction layers for one model request."""

    def build(self, host, messages: list) -> list:
        return self.freeze(host, messages).project(messages)

    def freeze(self, host, messages: list) -> FrozenRequestSnapshot:
        """Consume dynamic notes once; candidate history projection stays pure."""
        tools = expose_specs(host._active_tool_specs())
        capabilities = getattr(host, "model_capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            tools = []
        recovery = getattr(host, "recovery", None)
        repeat_notice = (recovery.repeat_notice_snapshot(
            host.runtime_state, getattr(host, "active_run_context", None),
            host.current_agent_name, messages,
        ) if recovery is not None and getattr(host, "active_run_context", None) is not None else None)

        def snapshot(system, dynamic="", reminder=""):
            return FrozenRequestSnapshot(
                system=system,
                dynamic=dynamic,
                instruction_reminder=reminder,
                source_version=message_version(messages),
                project_history=host._sanitize_messages,
                _tools=tuple(copy.deepcopy(tools)),
                repeat_notice=repeat_notice,
                memory_scope_id=getattr(getattr(host, "_mm", None), "scope_id", ""),
                memory_generation=(getattr(host, "_last_memory_cache_key", None) or (None, None))[1],
            )

        if getattr(host, "_structured_output_active_repair", ""):
            repeat_notice = None
            host.tracer.log(
                "context_layers",
                stable_tokens=estimate_text_tokens(
                    STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
                ),
                dynamic_tokens=0,
                scratch_tokens=0,
                repair=True,
            )
            return snapshot(STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT)
        if getattr(host, "runtime_profile", "") in {"read_child", "write_child"}:
            facts = (build_current_facts(host.runtime_state, workspace=current_workdir())
                     if has_authoritative_task(messages, host.runtime_state) else "")
            facts = "\n".join(part for part in (facts, render_task_assessment(host.runtime_state)) if part)
            memory = host._memory_block(_task_query(host, messages))
            if facts or repeat_notice or memory:
                stable, dynamic, stats = build_context_layers(
                    host.system_prompt, memory, "", "", current_run_settings().system_context_budget_tokens,
                    essential_block=facts,
                    reserved_block=repeat_notice.text if repeat_notice else "",
                )
                host.tracer.log("context_layers", child_profile=True, **stats)
                return snapshot(stable, dynamic)
            host.tracer.log("context_layers", child_profile=True,
                            stable_tokens=estimate_text_tokens(host.system_prompt),
                            dynamic_tokens=0, scratch_tokens=0)
            return snapshot(host.system_prompt)

        task_query = _task_query(host, messages)
        handoff = (render_execution_handoff(host.runtime_state, workspace=current_workdir())
                   if getattr(host.runtime_state, "execution_handoff", None) else "")
        authoritative = has_authoritative_task(messages, host.runtime_state)
        facts = (build_current_facts(host.runtime_state, workspace=current_workdir(),
                                    run_evidence=getattr(host, "run_evidence", None)) if authoritative else handoff)
        # 版本头与模型判断完整纳入请求硬预算，不能被可选背景裁剪。
        facts = "\n".join(part for part in (facts, render_task_assessment(host.runtime_state),
                                          render_working_plan_revision(host.runtime_state)) if part)
        scratch = host._sp.build_prompt_block()
        plan_text = getattr(host.runtime_state, "plan_text", "")
        working_plan = (
            "<working-plan>\nStage route (advisory). Current user requirements and current-task-facts take precedence. "
            "An accepted pending execution-handoff records the handed-off action; revising this route does not replace it. "
            "Without a handoff, choose tools normally.\n" + plan_text + "\n</working-plan>"
        ) if plan_text else ""
        memory = host._memory_block(task_query)
        progress_nudges = host.runtime_state.strict_progress_nudges
        state = (host.runtime_state.build_prompt_block(strict=strict_local_tools(), include_current_facts=False)
                 if authoritative else host.runtime_state.build_prompt_block(strict=strict_local_tools()))
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
                working_plan,
                host._hook_prompt_block(),
                host._lineage_recovery_block(messages),
                (host._implementation_bundle_block(task_query, include_requirements=False)
                 if authoritative else host._implementation_bundle_block(task_query)),
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
            max_tokens=current_run_settings().system_context_budget_tokens,
            essential_block=facts,
            reserved_block=repeat_notice.text if repeat_notice else "",
        )
        run_snapshot = getattr(host, "config_snapshot", None)
        instructions = (
            load_instruction_context(
                current_workdir(), config_snapshot=run_snapshot,
            )
            if run_snapshot is not None
            else load_instruction_context(current_workdir())
        )
        frozen = snapshot(stable_system, dynamic_context, instructions.reminder)
        if any(tool.get("function", {}).get("name") == "update_scratchpad" for tool in tools):
            frozen = replace(frozen, working_memory_revision=host._sp.revision)
        api_messages = frozen.project(messages)
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
        visible_tools = frozen.tools
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
        return frozen


def build_context_layers(
    system_prompt: str,
    memory_block: str,
    state_block: str,
    scratch_block: str,
    max_tokens: int,
    *,
    essential_block: str = "",
    reserved_block: str = "",
) -> tuple[str, str, dict]:
    memory = memory_block or ""
    state = state_block or ""
    scratch = scratch_block or ""
    before = {
        "fixed_tokens": estimate_text_tokens(system_prompt),
        "memory_tokens": estimate_text_tokens(memory),
        "state_tokens": estimate_text_tokens(state),
        "scratch_tokens": estimate_text_tokens(scratch),
        "essential_tokens": estimate_text_tokens(essential_block),
        "reserved_tokens": estimate_text_tokens(reserved_block),
    }
    prefix = ("<context-injection>\n"
              "Recalled background memory, runtime state, and working memory for this turn. "
              "Treat as context, not as a new user request.\n")
    closing = "\n</context-injection>"
    envelope = estimate_text_tokens(prefix + closing) + 4
    before["envelope_tokens"] = envelope if any((memory, state, scratch, essential_block)) else 0
    max_tokens = max(1000, int(max_tokens or 6000))
    # 展示目标只裁可选背景；固定指令、必要事实和全部摘要由完整请求硬预算检查。
    optional_budget = max_tokens - estimate_text_tokens(essential_block) - estimate_text_tokens(reserved_block) - envelope
    if sum(before.values()) > max_tokens:
        used = sum(estimate_text_tokens(item) for item in (system_prompt, state, scratch))
        memory = truncate_text_tokens(memory, max(0, optional_budget - used))
        used = sum(estimate_text_tokens(item) for item in (system_prompt, memory, scratch))
        state = truncate_text_tokens(state, max(0, optional_budget - used))
    dynamic_parts = [part for part in (memory, state, scratch, essential_block) if part]
    dynamic_context = ""
    if dynamic_parts:
        dynamic_context = (
            prefix + "\n".join(dynamic_parts) + closing
        )
    after = {
        "fixed_tokens": estimate_text_tokens(system_prompt),
        "memory_tokens": estimate_text_tokens(memory),
        "state_tokens": estimate_text_tokens(state),
        "scratch_tokens": estimate_text_tokens(scratch),
        "essential_tokens": estimate_text_tokens(essential_block),
        "reserved_tokens": estimate_text_tokens(reserved_block),
        "envelope_tokens": envelope if dynamic_parts else 0,
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
    # 动态状态只放在当前请求末尾，避免更新时改写整段历史的缓存前缀。
    return [*messages, {"role": "user", "content": dynamic_context}]


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
    # 混合中英文不能用 token * 4 当字符数；二分选择仍在原预算内的片段。
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        selected = text[-middle:] if keep_tail else text[:middle]
        candidate = marker + selected if keep_tail else selected + marker
        if estimate_text_tokens(candidate) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    selected = (text[-low:] if keep_tail else text[:low]) if low else ""
    return marker + selected if keep_tail else selected + marker


def _task_query(host, messages: list) -> str:
    """Return one authoritative query for all task-aware prompt layers."""
    query = continuation_task_text(
        messages,
        canonical_task=getattr(host.runtime_state, "initial_task_text", ""),
    )
    if not query:
        query = getattr(host.runtime_state, "initial_task_text", "")
    # 任务常被运行说明包裹；截取前缀会丢掉正文中的文件和符号。
    return query if isinstance(query, str) else ""
