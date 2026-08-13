"""Production guardrail policy for Agent input, output, and tool boundaries."""
from __future__ import annotations

import asyncio
import copy

from nz_coder.runtime.guardrails import (
    GuardrailBlockedError,
    GuardrailEscalateError,
    validate_verdict,
)
from nz_coder.tool_executor import ToolExecutionResult, is_transactional_write_tool


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
        self, host, tool_call: dict, messages: list[dict],
    ) -> tuple[dict, ToolExecutionResult | None]:
        guardrails = self._selected(host, "tool")
        if not guardrails:
            return tool_call, None
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
                return current, ToolExecutionResult(
                    name=name,
                    tool_input=host._best_effort_tool_input(
                        current.get("function", {}).get("arguments", {})
                    ),
                    output=f"[Guardrail {guardrail.name}] {verdict['reason']}",
                    executed=False,
                    dispatch_failed=True,
                    command_failed=False,
                    is_write=is_transactional_write_tool(name),
                    permission_denied=False,
                    metadata={"guardrail": guardrail.name, "agent": host.current_agent_name},
                )
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "tool", verdict["reason"])
        return current, None

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
                result.output = f"[Guardrail {guardrail.name}] {verdict['reason']}"
                result.dispatch_failed = True
                result.command_failed = False
                result.permission_denied = True
            elif action == "escalate":
                raise GuardrailEscalateError(guardrail.name, "tool", verdict["reason"])
        return result

    @staticmethod
    def _selected(host, kind: str) -> tuple[object, ...]:
        graph = getattr(host, "agent_graph", None)
        if graph is None:
            return ()
        return tuple(
            guardrail
            for guardrail in graph.agent(graph.start).guardrails
            if getattr(guardrail, "kind", None) == kind
        )

    @staticmethod
    async def _await(value):
        return await value if asyncio.iscoroutine(value) else value

    @staticmethod
    def _trace(host, guardrail: object, hook_point: str, verdict: dict) -> None:
        host.tracer.log(
            "agent_guardrail",
            guardrail=str(getattr(guardrail, "name", "unknown")),
            hook_point=hook_point,
            decision=str(verdict.get("action") or "error"),
            agent=host.current_agent_name,
            reason=str(verdict.get("reason") or "")[:1000],
        )
