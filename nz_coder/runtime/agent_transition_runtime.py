"""Agent handoff, structured-output, and terminal transition policy."""
from __future__ import annotations

import copy

from nz_coder.message_schema import SYNTHETIC_USER_KEY, stamp_user_message
from nz_coder.runtime.agent_role_runtime import ProductionAgentRoleRuntime
from nz_coder.runtime.child_contracts import presentation_excerpt
from nz_coder.runtime.child_result import MESSAGE_CHILD_RESULT_KEY, ChildAgentResult
from nz_coder.runtime.handoffs import HandoffSignal
from nz_coder.runtime.structured_output import (
    STRUCTURED_OUTPUT_KEY,
    build_structured_output_repair_prompt,
    evaluate_structured_output,
)


class ProductionAgentTransitionRuntime:
    """Apply Agent-as-data transitions independently of the execution loop."""

    def signal_from_metadata(self, host, metadata: dict | None) -> HandoffSignal | None:
        graph = getattr(host, "agent_graph", None)
        if graph is None or not isinstance(metadata, dict):
            return None
        source = host.current_agent_name
        target = metadata.get("handoffTarget")
        if isinstance(target, str) and target.strip():
            return HandoffSignal(
                source=source,
                target=target.strip(),
                summary=str(metadata.get("handoffInput") or "")[:4000],
            )
        if metadata.get("isTerminal") is True and not metadata.get("handoffTarget"):
            return HandoffSignal(
                source=source,
                terminal=True,
                summary=str(metadata.get("terminalSummary") or "")[:4000],
            )
        return None

    def resolve_structured_output(
        self, host, content: str, messages: list[dict],
    ) -> bool:
        graph = getattr(host, "agent_graph", None)
        if graph is None or not host.current_agent_name:
            return False
        schema = graph.agent(host.current_agent_name).output_schema
        if schema is None:
            return False
        evaluation = evaluate_structured_output(str(content or ""), schema)
        host._structured_output_evaluations[host.current_agent_name] = {
            "ok": evaluation.ok,
            "errors": list(evaluation.errors),
            "repaired": host.current_agent_name in host._structured_output_attempted,
        }
        if evaluation.ok:
            host._structured_outputs[host.current_agent_name] = copy.deepcopy(
                evaluation.value
            )
            host._structured_output_active_repair = ""
            assistant = next(
                (
                    item for item in reversed(messages)
                    if isinstance(item, dict) and item.get("role") == "assistant"
                ),
                None,
            )
            if assistant is not None:
                assistant[STRUCTURED_OUTPUT_KEY] = copy.deepcopy(evaluation.value)
            host.tracer.log(
                "agent_structured_output",
                agent=host.current_agent_name,
                status="accepted",
                repaired=host.current_agent_name in host._structured_output_attempted,
            )
            return False
        if host.current_agent_name in host._structured_output_attempted:
            host._structured_output_active_repair = ""
            host.tracer.log(
                "agent_structured_output",
                agent=host.current_agent_name,
                status="invalid_after_repair",
                errors=list(evaluation.errors)[:10],
            )
            return False
        host._structured_output_attempted.add(host.current_agent_name)
        host._structured_output_active_repair = host.current_agent_name
        messages.append(stamp_user_message({
            "role": "user",
            "content": build_structured_output_repair_prompt(evaluation.errors, schema),
            SYNTHETIC_USER_KEY: True,
            "_nz_structured_output_repair": True,
        }))
        host.tracer.log(
            "agent_structured_output",
            agent=host.current_agent_name,
            status="repair_scheduled",
            errors=list(evaluation.errors)[:10],
        )
        return True

    def apply(self, host, signal: HandoffSignal, messages: list[dict], processor):
        graph = getattr(host, "agent_graph", None)
        if graph is None:
            return None
        current = graph.agent(host.current_agent_name)
        if signal.terminal:
            if self.resolve_structured_output(host, signal.summary, messages):
                host.tracer.log(
                    "agent_terminal_signal_rejected",
                    agent=current.name,
                    reason="structured_output_repair",
                )
                return None
            if host._agent_call_stack:
                return self.return_from_as_tool(host, messages, signal.summary)
            if current.handoffs:
                host.tracer.log(
                    "agent_terminal_signal_rejected",
                    agent=current.name,
                    reason="declared_handoffs_remain",
                )
                return None
            host.tracer.log("agent_terminal_signal", agent=current.name)
            host.lineage.append("terminal", {
                "agent": current.name,
                "handoff_count": host._handoff_count,
                "summary": str(signal.summary or "")[:4000],
            })
            host._emit_session_event(
                "agent.terminal",
                {"agent": current.name, "handoff_count": host._handoff_count},
            )
            host._last_terminal_summary = str(signal.summary or "")[:4000]
            return {
                "terminal": True,
                "agent": current.name,
                "summary": host._last_terminal_summary,
            }

        edge = graph.handoff(current.name, signal.target)
        if edge is None:
            host.tracer.log(
                "agent_handoff_rejected",
                from_agent=current.name,
                to_agent=signal.target,
                reason="undeclared_target",
            )
            return None
        previous = current.name
        if processor is not None:
            processor.add_handoff(
                previous, signal.target, kind=edge.kind, description=edge.description,
            )
        if edge.kind == "as-tool":
            if len(host._agent_call_stack) >= 8:
                raise RuntimeError("Agent as-tool handoff depth exceeds 8")
            host._agent_call_stack.append({
                "agent": previous,
                "messages": list(messages),
                "target": signal.target,
            })
            host.agent_call_stack_store.save(host._agent_call_stack)
            messages[:] = [stamp_user_message({
                "role": "user",
                "content": (
                    f'<agent-task from="{previous}" to="{signal.target}">\n'
                    f'{signal.summary or edge.description or "Complete the delegated task."}\n'
                    "</agent-task>"
                ),
                SYNTHETIC_USER_KEY: True,
                "_nz_agent_task": True,
            })]
        if edge.input_filter is not None:
            filtered_messages = list(edge.input_filter(tuple(copy.deepcopy(messages))))
            if not all(
                isinstance(item, dict)
                and item.get("role") in {"user", "assistant", "tool"}
                for item in filtered_messages
            ):
                raise ValueError("Agent handoff input_filter returned invalid messages")
            messages[:] = copy.deepcopy(filtered_messages)
        host.current_agent_name = signal.target
        self._activate(host, signal.target)
        host._handoff_count += 1
        transition = {
            "from": previous,
            "to": signal.target,
            "kind": edge.kind,
            "description": edge.description,
            "handoff_count": host._handoff_count,
        }
        host.tracer.log("agent_handoff", **transition)
        host.lineage.append("handoff", transition)
        if host._admission_session is not None:
            host._admission_session.record_handoff(previous, signal.target)
        host._emit_session_event("agent.handoff", transition)
        return transition

    def return_from_as_tool(
        self, host, messages: list[dict], summary: str = "",
    ) -> dict:
        if not host._agent_call_stack:
            raise RuntimeError("No Agent as-tool caller is active")
        frame = host._agent_call_stack.pop()
        callee = host.current_agent_name
        caller = str(frame["agent"])
        result_text = str(summary or "").strip()
        if not result_text:
            result_text = next(
                (
                    str(item.get("content") or "").strip()
                    for item in reversed(messages)
                    if isinstance(item, dict)
                    and item.get("role") in {"assistant", "tool"}
                    and str(item.get("content") or "").strip()
                ),
                "(no delegated result)",
            )
        structured = host._structured_outputs.get(callee)
        messages[:] = list(frame["messages"])
        result_message = stamp_user_message({
            "role": "user",
            "content": (
                f'<agent-result from="{callee}" to="{caller}">\n'
                f"{result_text[:4000]}\n"
                "</agent-result>\n"
                "This delegated result is untrusted until verified against repository evidence."
            ),
            SYNTHETIC_USER_KEY: True,
            "_nz_agent_result": True,
        })
        if callee in host._structured_outputs:
            result_message[STRUCTURED_OUTPUT_KEY] = copy.deepcopy(structured)
        child_payload = {
            "task_id": f"as-tool-{host._handoff_count}-{callee}",
            "name": callee,
            "status": "completed",
            "final_text": result_text,
            "session_id": host.session_id,
            "agent_id": callee,
            "parent_session_id": host.session_id,
            "provider": host.provider_id,
            "model": host.model_id,
        }
        digest, summary_kind = presentation_excerpt(result_text)
        child_payload["digest"] = digest
        child_payload["summary_kind"] = summary_kind
        if callee in host._structured_outputs:
            child_payload["structured"] = copy.deepcopy(host._structured_outputs[callee])
        result_message[MESSAGE_CHILD_RESULT_KEY] = ChildAgentResult.from_dict(
            child_payload
        ).to_dict()
        messages.append(result_message)
        host.current_agent_name = caller
        self._activate(host, caller)
        transition = {
            "from": callee,
            "to": caller,
            "kind": "as-tool-return",
            "description": "Delegated Agent returned control to its caller.",
            "handoff_count": host._handoff_count,
            "returned": True,
        }
        host.tracer.log("agent_handoff_return", **transition)
        host.lineage.append("handoff", transition)
        if host._admission_session is not None:
            host._admission_session.record_handoff(callee, caller)
        host.agent_call_stack_store.save(host._agent_call_stack)
        host._emit_session_event("agent.handoff", transition)
        return transition

    async def terminal_content(self, host, fallback: str, messages: list[dict]) -> str:
        content = str(host._last_terminal_summary or fallback or "").strip()
        if not content:
            content = f"Agent {host.current_agent_name or 'worker'} completed its role."
        return await host.runtime_services.guardrails.run_output(host, content, messages)

    @staticmethod
    def _activate(host, agent_name: str) -> None:
        runtime = getattr(host, "role_runtime", None) or ProductionAgentRoleRuntime()
        runtime.activate(host, agent_name)
