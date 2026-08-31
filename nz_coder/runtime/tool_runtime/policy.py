"""Tool admission, scheduling, convergence, and observability policy."""
from __future__ import annotations

import json
import time

from nz_coder.foundation import config
from nz_coder.tool_platform.command_policy import is_known_read_only_command
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.agent.admission import resolve_tool_capability
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.execution_context import max_parallel_tasks, strict_local_tools
from nz_coder.runtime.core.tool_context import ToolPolicyContext
from nz_coder.swebench.policy import strict_private_tool_input_violation
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    is_transactional_write_tool,
)
from nz_coder.tools import get_execution_mode
from nz_coder.state.skills import current_skill_allowed_tools


_STALL_SIDECAR_EXEMPT_TOOLS = frozenset({
    "diff_status",
    "verify_changed_files",
})


class ProductionToolPolicy:
    """Own policy decisions consumed by the canonical ToolRuntime."""

    def tool_batch_has_write(self, context: ToolPolicyContext, tool_calls_raw: list) -> bool:
        """只按实际会执行的调用判断是否需要开启事务。"""
        will_execute = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
        return any(
            is_transactional_write_tool(tc["function"]["name"])
            for tc in will_execute
        )


    def tool_call_can_run_concurrently(self, context: ToolPolicyContext, tool_call: dict) -> bool:
        """Return whether a tool call explicitly opts into read concurrency."""
        tool_name = tool_call["function"]["name"]
        tool_input = context.parse_input(
            tool_call["function"].get("arguments", {}),
        )
        if tool_name == "bash":
            return is_known_read_only_command(str(tool_input.get("command") or ""))
        if tool_name != "task":
            return get_execution_mode(tool_name) == "read"
        agent_type = str(tool_input.get("agent_type") or "explore").strip().lower()
        aliases = {"review": "plan", "test": "plan", "critic": "reflection"}
        normalized = aliases.get(agent_type, agent_type)
        return normalized in {"explore", "plan", "reflection"}


    def agent_tool_rejections(
        self, context: ToolPolicyContext, tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Fail closed when the current Agent invokes an undeclared tool."""
        graph = context.agent_graph
        tool_allowlist = context.tool_allowlist
        skill_allowed = current_skill_allowed_tools()
        if graph is None and tool_allowlist is None and skill_allowed is None:
            return {}
        if graph is not None:
            agent = graph.agent(context.agent_name)
            allowed = (
                None
                if agent.allowed_tools is None
                else set(agent.allowed_tools) | {"emit_handoff"}
            )
            agent_name = agent.name
        else:
            allowed = None
            agent_name = context.agent_name
        if tool_allowlist is not None:
            allowed = set(tool_allowlist) if allowed is None else allowed & set(tool_allowlist)
        if skill_allowed is not None:
            skill_surface = set(skill_allowed) | {"load_skill", "tool_search", "question"}
            allowed = skill_surface if allowed is None else allowed & skill_surface
        if allowed is None:
            return {}
        rejected: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(tool_calls):
            name = str(tool_call.get("function", {}).get("name") or "")
            if name in allowed:
                continue
            tool_input = context.parse_input(
                tool_call.get("function", {}).get("arguments", {}),
            )
            governed_by_skill = skill_allowed is not None and name not in skill_surface
            output = (
                f"Denied: active Skill policy does not allow tool '{name}'."
                if governed_by_skill else
                f"Denied: Agent role '{agent_name}' may not call undeclared tool '{name}'."
            )
            rejected[index] = ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output=output,
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=True,
                title="Agent tool guardrail",
                metadata={
                    "agent": agent_name,
                    "guardrail": (
                        "skill_allowed_tools" if governed_by_skill else "declared_tools"
                    ),
                },
            )
        return rejected


    def admission_tool_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Re-apply the system ceiling to concrete calls at execution time."""
        handle = context.admission_handle
        if handle is None:
            return {}
        allowed = handle.system_cap.effective_capabilities()
        rejected: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(tool_calls):
            function = tool_call.get("function", {})
            name = str(function.get("name") or "")
            tool_input = context.parse_input(
                function.get("arguments", {}),
            )
            if (
                name == "emit_handoff"
                and tool_input.get("terminal") is True
                and not str(tool_input.get("target") or "").strip()
            ):
                continue
            capability = resolve_tool_capability(name, tool_input)
            if capability in allowed:
                continue
            output = (
                "[Invariant toolPermission] "
                f"Agent '{context.agent_name}' may not use capability "
                f"'{capability}' through tool '{name}'."
            )
            rejected[index] = ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output=output,
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=False,
                title="Agent capability invariant",
                metadata={
                    "agent": context.agent_name,
                    "invariant": "toolPermission",
                    "capability": capability,
                },
            )
        return rejected


    def strict_progress_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Keep the legacy hook as an advisory-only compatibility no-op."""
        return {}


    def strict_private_path_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Keep SWE inference away from NZ-Coder traces and runtime state."""
        if not strict_local_tools():
            return {}
        rejected: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(tool_calls):
            function = tool_call.get("function", {})
            name = str(function.get("name") or "")
            tool_input = context.parse_input(function.get("arguments", {}))
            violation = strict_private_tool_input_violation(name, tool_input)
            if not violation:
                continue
            rejected[index] = ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output=f"Denied: {violation}.",
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=True,
                title="Strict private path guardrail",
                metadata={
                    "guardrail": "strict_private_path",
                },
            )
            context.trace(
                "strict_private_path_blocked",
                name=name,
            )
        return rejected


    def implementation_phase_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Keep the retired pre-edit phase gate as a compatibility no-op.

        Investigation convergence is advisory. Safety, explicit task constraints,
        exact repetition detection, and the run-level iteration cap remain enforced
        by their dedicated policies.
        """
        del context, tool_calls
        return {}


    def task_constraint_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Enforce explicit artifact immutability declared by the user."""
        action_for = getattr(context.runtime_state, "task_constraint_action", None)
        if not callable(action_for):
            return {}
        rejected: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(tool_calls):
            function = tool_call.get("function", {})
            name = str(function.get("name") or "")
            tool_input = context.parse_input(function.get("arguments", {}))
            if action_for(name, tool_input) == "allow":
                continue
            rejected[index] = ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output=(
                    "Denied: the current user task explicitly forbids modifying test "
                    "files. Keep the existing tests unchanged and repair source code only."
                ),
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=False,
                title="Task constraint gate",
                metadata={
                    "guardrail": "task_constraint",
                    "constraint": "tests_immutable",
                },
            )
            context.trace(
                "task_constraint_blocked",
                name=name,
                constraint="tests_immutable",
            )
        return rejected


    def closure_phase_rejections(
        self,
        context: ToolPolicyContext,
        tool_calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Enforce environment and shell-observation safety facts."""
        state = context.runtime_state
        action_for = getattr(state, "closure_phase_action", None)
        decision_for = getattr(state, "closure_phase_decision", None)
        if not callable(action_for):
            return {}
        rejected: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(tool_calls):
            function = tool_call.get("function", {})
            name = str(function.get("name") or "")
            tool_input = context.parse_input(function.get("arguments", {}))
            if callable(decision_for):
                action, reason = decision_for(name, tool_input)
            else:
                action, reason = action_for(name, tool_input), ""
            if action == "allow":
                continue
            guardrail = "shell_write_boundary"
            rejected[index] = ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output=(
                    "Denied: this workspace is not Git-backed, so `git diff/status` "
                    "cannot add closure evidence. Use diff_status and "
                    "verify_changed_files instead."
                    if reason == "git_required_but_unavailable" else
                    "Denied: shell write safety blocked a compound or mutating Git "
                    "observation. Run a single read-only git diff/status command."
                ),
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=False,
                title="Shell write safety gate",
                metadata={
                    "guardrail": guardrail,
                    "reason": reason,
                    "work_phase": str(getattr(state, "work_phase", "")),
                    "mutation_generation": int(
                        getattr(state, "mutation_generation", 0) or 0
                    ),
                },
            )
            context.trace(
                "shell_write_blocked",
                name=name,
                work_phase=str(getattr(state, "work_phase", "")),
                reason=reason,
            )
        return rejected


    def begin_tool_batch(
        self, context: ToolPolicyContext, tool_calls: list, has_write: bool,
    ) -> tuple[str, float]:
        """Create a stable batch id and emit the scheduling input facts."""
        batch_id = context.next_batch_id()
        started = time.perf_counter()
        if context.trace is not None:
            context.trace(
                "tool_batch_started",
                batch_id=batch_id,
                call_count=len(tool_calls),
                has_write=has_write,
                names=[call["function"]["name"] for call in tool_calls],
                parallel_limit=max_parallel_tasks(),
            )
        return batch_id, started


    def finish_tool_batch_observation(
        self,
        context: ToolPolicyContext,
        *,
        batch_id: str,
        started: float,
        mode: str,
        dispatched: list,
        segments: list[dict],
        error: str = "",
    ) -> None:
        """Emit per-segment and aggregate scheduling facts without affecting execution."""
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        if not segments and dispatched:
            segments = [{
                "segment_index": 0,
                "kind": "sequential_guarded",
                "call_count": len(dispatched),
                "names": [tc["function"]["name"] for _i, tc, _result in dispatched],
                "duration_ms": wall_ms,
                "peak_concurrency": 1,
                "barrier_wait_ms": 0.0,
                "max_queue_wait_ms": max(
                    (float(getattr(result, "queue_wait_ms", 0.0) or 0.0)
                     for _i, _tc, result in dispatched),
                    default=0.0,
                ),
            }]

        peak = max(
            (int(segment.get("peak_concurrency", 1) or 1) for segment in segments),
            default=(1 if dispatched else 0),
        )
        barrier_wait_ms = round(sum(
            float(segment.get("barrier_wait_ms", 0.0) or 0.0)
            for segment in segments
        ), 3)
        parallel_segments = sum(1 for segment in segments if segment.get("kind") == "parallel_read")
        serial_segments = sum(1 for segment in segments if segment.get("kind") != "parallel_read")
        total_call_ms = round(sum(
            float(getattr(result, "duration_ms", 0.0) or 0.0)
            for _i, _tc, result in dispatched
        ), 3)

        observability = context.observability
        observability["batches"] += 1
        observability["calls"] += len(dispatched)
        observability["wall_ms"] = round(observability["wall_ms"] + wall_ms, 3)
        observability["tool_duration_ms"] = round(
            observability["tool_duration_ms"] + total_call_ms, 3,
        )
        observability["peak_concurrency"] = max(observability["peak_concurrency"], peak)
        observability["parallel_segments"] += parallel_segments
        observability["serial_segments"] += serial_segments
        observability["barrier_wait_ms"] = round(
            observability["barrier_wait_ms"] + barrier_wait_ms, 3,
        )

        for segment in segments:
            context.trace("tool_schedule_segment", batch_id=batch_id, **segment)
        context.trace(
            "tool_batch_completed",
            batch_id=batch_id,
            mode=mode,
            call_count=len(dispatched),
            wall_ms=wall_ms,
            total_call_ms=total_call_ms,
            peak_concurrency=peak,
            parallel_segments=parallel_segments,
            serial_segments=serial_segments,
            barrier_wait_ms=barrier_wait_ms,
            error=error or None,
        )


    def trace_tool_streak_reset(self, context: ToolPolicyContext) -> None:
        """Publish a consumed RecoveryState reset event and update run totals."""
        event = context.recovery.consume_tool_streak_event()
        if not event:
            return
        observability = context.observability
        observability["streak_resets"] += 1
        context.trace("doom_loop_streak_reset", **event)


    def find_repeated_tool_calls(
        self, context: ToolPolicyContext, tool_calls: list,
    ) -> dict[int, ToolExecutionResult]:
        """Compose InfCode's immediate guard with InfCodeX's async sidecar."""
        blocked: dict[int, ToolExecutionResult] = {}
        recovery = context.recovery
        if recovery is None:
            recovery = RecoveryState()
            context.recovery = recovery
        threshold = config.DOOM_LOOP_THRESHOLD
        for index, tool_call in enumerate(tool_calls):
            fn_name = tool_call["function"]["name"]
            raw_arguments = tool_call["function"].get("arguments", {})
            tool_input = context.parse_input(raw_arguments)
            signature_input: object = tool_input
            if isinstance(raw_arguments, str):
                try:
                    signature_input = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    signature_input = {"invalid_json": raw_arguments}

            # Runtime-owned verification calls are synthesized by the
            # scheduler, not selected by the model.  InfCodeX wires its
            # detector at the assistant tool-use boundary, so these calls
            # never enter either L1 repeat tracking or the L2 stall sidecar.
            # Keep any pending model nudge queued for the next actual model
            # tool call as well.
            tool_call_id = str(tool_call.get("id") or "")
            runtime_owned_contract = bool(
                isinstance(signature_input, dict)
                and signature_input.get("_nz_runtime_contract") is True
                and tool_call_id.startswith("verification-contract-")
            )
            runtime_owned_stage = bool(
                isinstance(signature_input, dict)
                and signature_input.get("_nz_runtime_verification_stage")
                in {"static", "targeted"}
                and tool_call_id.startswith("verification-stage-")
            )
            if runtime_owned_contract or runtime_owned_stage:
                context.trace(
                    "runtime_owned_stall_observation_skipped",
                    name=fn_name,
                    tool_call_id=tool_call_id,
                )
                continue

            orchestrator = context.stall_orchestrator
            pending_nudge = (
                orchestrator.consume_pending_nudge()
                if orchestrator is not None
                else None
            )
            if pending_nudge is not None:
                blocked[index] = ToolExecutionResult(
                    name=fn_name,
                    tool_input=tool_input,
                    output=pending_nudge,
                    executed=False,
                    dispatch_failed=True,
                    command_failed=False,
                    is_write=is_transactional_write_tool(fn_name),
                    permission_denied=False,
                    metadata={"stall_nudge": True, "stall_kind": "sidecar"},
                )
                if context.trace is not None:
                    context.trace(
                        "stall_sidecar_nudge_consumed",
                        name=fn_name,
                        tool_call_id=str(tool_call.get("id") or ""),
                    )
                continue

            if orchestrator is not None and fn_name in _STALL_SIDECAR_EXEMPT_TOOLS:
                context.trace(
                    "stall_sidecar_observation_skipped",
                    name=fn_name,
                    reason="deterministic_closure_tool",
                )
            elif orchestrator is not None:
                signaled = orchestrator.record_tool_use({
                    "id": str(tool_call.get("id") or ""),
                    "name": fn_name,
                    "input": signature_input,
                })
                if signaled:
                    context.trace("stall_l1_signal", name=fn_name)
            observation = recovery.observe_tool_call(
                fn_name,
                signature_input,
                threshold=threshold,
            )
            self.trace_tool_streak_reset(context)
            if not observation["should_block"]:
                continue
            count = observation["count"]
            effective_threshold = max(2, threshold)
            output = (
                "Denied: Doom loop detected: identical call to "
                f"`{fn_name}` repeated {count} times in the recent tool window "
                f"(threshold {effective_threshold}). Use the evidence already returned; "
                "stop re-reading and synthesize the answer or change the approach."
            )
            blocked[index] = ToolExecutionResult(
                name=fn_name,
                tool_input=tool_input,
                output=output,
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(fn_name),
                permission_denied=True,
                metadata={
                    "stall_kind": "consecutive",
                },
            )
            if context.trace is not None:
                context.trace(
                    "doom_loop_blocked",
                    name=fn_name,
                    count=count,
                    threshold=effective_threshold,
                    kind="consecutive",
                )
        return blocked


    def resolve_doom_loop_permissions(
        self,
        context: ToolPolicyContext,
        blocked: dict[int, ToolExecutionResult],
        tool_calls: list,
    ) -> dict[int, ToolExecutionResult]:
        """Allow an interactive user to override an exact-repeat guard."""
        remaining = dict(blocked)
        for index in tuple(remaining):
            result = remaining[index]
            metadata = {"tool": result.name, "input": result.tool_input}
            if context.permissions.ask_special("doom_loop", metadata):
                remaining.pop(index, None)
                context.recovery.reset_tool_call_history(reason="doom_loop_approved")
                self.trace_tool_streak_reset(context)
                context.trace(
                    "doom_loop_approved",
                    name=result.name,
                    tool_call_id=str(tool_calls[index].get("id") or ""),
                )
        return remaining


    async def resolve_doom_loop_permissions_async(
        self,
        context: ToolPolicyContext,
        blocked: dict[int, ToolExecutionResult],
        tool_calls: list,
    ) -> dict[int, ToolExecutionResult]:
        """Async wrapper that keeps terminal permission UI off the event loop."""
        remaining = dict(blocked)
        for index in tuple(remaining):
            result = remaining[index]
            metadata = {"tool": result.name, "input": result.tool_input}
            allowed = await _to_thread_settled(
                context.permissions.ask_special,
                "doom_loop",
                metadata,
            )
            if allowed:
                remaining.pop(index, None)
                context.recovery.reset_tool_call_history(reason="doom_loop_approved")
                self.trace_tool_streak_reset(context)
                context.trace(
                    "doom_loop_approved",
                    name=result.name,
                    tool_call_id=str(tool_calls[index].get("id") or ""),
                )
        return remaining


def _empty_tool_observability() -> dict:
    return {
        "batches": 0,
        "calls": 0,
        "wall_ms": 0.0,
        "tool_duration_ms": 0.0,
        "peak_concurrency": 0,
        "parallel_segments": 0,
        "serial_segments": 0,
        "barrier_wait_ms": 0.0,
        "streak_resets": 0,
    }
