"""Production guardrail policy for Agent input, output, and tool boundaries."""
from __future__ import annotations

import asyncio
import copy
import math

from nz_coder.runtime.agent.guardrails import (
    GuardrailBlockedError,
    GuardrailEscalateError,
    validate_verdict,
)
from nz_coder.runtime.agent.auto_mode import parse_tool_arguments
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    is_transactional_write_tool,
)


class ProductionGuardrailRuntime:
    """Evaluate declared guardrails without coupling policy to AgentLoop."""

    def has(self, host, kind: str) -> bool:
        """Return whether the entry Agent declares a guardrail hook."""
        return bool(self._selected(host, kind))

    async def run_input(self, host, messages: list[dict]) -> None:
        guardrails = self._selected(host, "input")
        if not guardrails:
            return
        current = copy.deepcopy(messages)
        for guardrail in guardrails:
            verdict = validate_verdict(
                await self._await(guardrail.check(
                    current,
                    {"agent": host.agent_graph.agent(host.agent_graph.start), "messages": current},
                )),
                guardrail.name,
            )
            self._trace(host, guardrail, "input", verdict)
            action = verdict["action"]
            if action == "rewrite":
                payload = verdict["payload"]
                if not isinstance(payload, (list, tuple)) or not all(
                    isinstance(item, dict)
                    and item.get("role") in {"user", "assistant", "tool"}
                    for item in payload
                ):
                    raise ValueError(
                        f'InputGuardrail "{guardrail.name}" rewrite requires messages'
                    )
                current = copy.deepcopy(list(payload))
            elif action == "block":
                raise GuardrailBlockedError(guardrail.name, "input", verdict["reason"])
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "input", verdict["reason"])
        messages[:] = current

    async def run_output(self, host, content: str, messages: list[dict]) -> str:
        current = {"role": "assistant", "content": str(content or "")}
        for guardrail in self._selected(host, "output"):
            verdict = validate_verdict(
                await self._await(guardrail.check(
                    copy.deepcopy(current),
                    {"agent": host.agent_graph.agent(host.agent_graph.start), "messages": messages},
                )),
                guardrail.name,
            )
            self._trace(host, guardrail, "output", verdict)
            action = verdict["action"]
            if action == "rewrite":
                payload = verdict["payload"]
                if (
                    not isinstance(payload, dict)
                    or payload.get("role") != "assistant"
                    or not isinstance(payload.get("content"), str)
                ):
                    raise ValueError(
                        f'OutputGuardrail "{guardrail.name}" rewrite requires assistant message'
                    )
                current = copy.deepcopy(payload)
            elif action == "block":
                raise GuardrailBlockedError(guardrail.name, "output", verdict["reason"])
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "output", verdict["reason"])
        return current["content"]

    async def before_tool(
        self,
        host,
        tool_call: dict,
        messages: list[dict],
        *,
        _include_auto: bool = True,
    ) -> tuple[dict, ToolExecutionResult | None]:
        guardrails = self._selected(host, "tool")
        current = copy.deepcopy(tool_call)
        for guardrail in guardrails:
            callback = getattr(guardrail, "before_tool", None)
            if not callable(callback):
                continue
            verdict = validate_verdict(
                await self._await(callback(
                    copy.deepcopy(current),
                    {"agent": host.agent_graph.agent(host.current_agent_name), "messages": messages},
                )),
                guardrail.name,
            )
            self._trace(host, guardrail, "tool", verdict)
            action = verdict["action"]
            if action == "rewrite":
                payload = verdict["payload"]
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("function"), dict)
                    or not isinstance(payload["function"].get("name"), str)
                ):
                    raise ValueError(
                        f'ToolGuardrail "{guardrail.name}" rewrite requires tool call'
                    )
                original_name = str(current.get("function", {}).get("name") or "")
                rewritten_name = str(payload["function"]["name"])
                if rewritten_name != original_name:
                    raise ValueError(
                        f'ToolGuardrail "{guardrail.name}" may rewrite arguments but not the tool name'
                    )
                current = copy.deepcopy(payload)
                current.setdefault("id", tool_call.get("id"))
            elif action == "block":
                name = str(current.get("function", {}).get("name") or "unknown")
                safe_call = copy.deepcopy(current)
                safe_call.setdefault("function", {})["arguments"] = "{}"
                return safe_call, ToolExecutionResult(
                    name=name,
                    tool_input={},
                    output=f'Tool blocked by guardrail "{guardrail.name}".',
                    executed=False,
                    dispatch_failed=True,
                    command_failed=False,
                    is_write=is_transactional_write_tool(name),
                    permission_denied=False,
                    metadata={
                        "guardrail": guardrail.name,
                        "agent": host.current_agent_name,
                        "reason_code": "policy_block",
                    },
                )
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "tool", verdict["reason"])

        if not _include_auto:
            return current, None
        controller = getattr(host, "auto_mode_controller", None)
        context_factory = getattr(host, "_auto_mode_context", None)
        context = context_factory() if callable(context_factory) else None
        if controller is None or context is None:
            return current, None

        function = current.get("function", {})
        name = str(function.get("name") or "unknown")
        tool_input = parse_tool_arguments(function.get("arguments", {}))
        if tool_input is None:
            return current, None
        admission = await controller.admit(
            context,
            name,
            tool_input,
            messages,
        )
        if not admission.allowed:
            safe_call = copy.deepcopy(current)
            safe_call.setdefault("function", {})["arguments"] = "{}"
            return safe_call, ToolExecutionResult(
                name=name,
                tool_input={},
                output="Denied: tool invocation rejected by policy.",
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_transactional_write_tool(name),
                permission_denied=True,
                metadata={
                    "guardrail": "auto_mode",
                    "agent": host.current_agent_name,
                    "source": admission.source,
                    "reason_code": admission.reason_code,
                    "action_digest": admission.action_digest,
                },
            )
        return current, None

    async def before_tool_sync(
        self,
        host,
        tool_call: dict,
        messages: list[dict],
    ) -> tuple[dict, ToolExecutionResult | None]:
        """Run declared guards without async Auto admission for sync callers."""
        return await self.before_tool(
            host,
            tool_call,
            messages,
            _include_auto=False,
        )

    async def after_tool(
        self, host, tool_call: dict, result: ToolExecutionResult, messages: list[dict],
    ) -> ToolExecutionResult:
        for guardrail in self._selected(host, "tool"):
            callback = getattr(guardrail, "after_tool", None)
            if not callable(callback):
                continue
            verdict = validate_verdict(
                await self._await(callback(
                    copy.deepcopy(tool_call),
                    {"content": result.output, "is_error": result.dispatch_failed or result.command_failed},
                    {"agent": host.agent_graph.agent(host.current_agent_name), "messages": messages},
                )),
                guardrail.name,
            )
            self._trace(host, guardrail, "tool", verdict)
            action = verdict["action"]
            if action == "rewrite":
                payload = verdict["payload"]
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                    raise ValueError(
                        f'ToolGuardrail "{guardrail.name}" rewrite requires tool result'
                    )
                result.output = payload["content"]
                result.dispatch_failed = bool(payload.get("is_error", False))
                result.command_failed = False
            elif action == "block":
                result.output = f'Tool result blocked by guardrail "{guardrail.name}".'
                result.dispatch_failed = True
                result.command_failed = False
                result.permission_denied = True
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "tool", verdict["reason"])
            if action in {"rewrite", "block"}:
                # The verdict admits only its replacement body, not aliases of
                # the original body in metadata, title or media attachments.
                # Keep execution facts in the result/ledger, separate from UI
                # payloads. An unmodified allow retains normal diagnostics.
                result.metadata = _admitted_control_metadata(host, result, action)
                result.title = ""
                result.attachments = []
        return result

    @staticmethod
    def _selected(host, kind: str) -> tuple[object, ...]:
        graph = getattr(host, "agent_graph", None)
        if graph is None:
            return ()
        agent_name = graph.start
        if kind == "tool":
            current_name = str(getattr(host, "current_agent_name", "") or "")
            if current_name in graph.names():
                agent_name = current_name
        return tuple(
            guardrail
            for guardrail in graph.agent(agent_name).guardrails
            if getattr(guardrail, "kind", None) == kind
        )

    @staticmethod
    async def _await(value):
        return await value if asyncio.iscoroutine(value) else value

    @staticmethod
    def _trace(host, guardrail: object, hook_point: str, verdict: dict) -> None:
        reason = str(verdict.get("reason") or "")
        host.tracer.log(
            "agent_guardrail",
            guardrail=str(getattr(guardrail, "name", "unknown")),
            hook_point=hook_point,
            decision=str(verdict.get("action") or "error"),
            agent=host.current_agent_name,
            # Output policy callbacks have access to the private Provider body.
            # Keep their audit record structural even if a policy accidentally
            # echoes that body into its reason string.
            reason="",
            reason_provided=bool(reason),
        )


def _admitted_control_metadata(host, result: ToolExecutionResult, action: str) -> dict:
    """Keep typed control facts, never the replaced tool's presentation aliases."""
    admitted: dict = {"guardrail_output_action": action}
    if action != "rewrite" or result.dispatch_failed or not result.executed:
        return admitted
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if result.name == "plan_exit":
        for name in ("plan_exit_approved", "plan_exit_terminal"):
            if type(metadata.get(name)) is bool:
                admitted[name] = metadata[name]
    elif result.name == "task":
        cost = metadata.get("child_cost_delta")
        if type(cost) in {float, int} and 0 <= cost < math.inf:
            admitted["child_cost_delta"] = cost
    elif result.name == "emit_handoff":
        graph = getattr(host, "agent_graph", None)
        source = getattr(host, "current_agent_name", "")
        if graph is None or source not in graph.names():
            return admitted
        target = metadata.get("handoffTarget")
        edge = graph.handoff(source, target) if isinstance(target, str) else None
        if edge is not None:
            admitted.update(handoffSource=source, handoffTarget=edge.target,
                            handoffKind=edge.kind, handoffInput=result.output[:4000])
        elif metadata.get("isTerminal") is True and not target and not graph.agent(source).handoffs:
            admitted.update(handoffSource=source, isTerminal=True,
                            terminalSummary=result.output[:4000])
    return admitted
