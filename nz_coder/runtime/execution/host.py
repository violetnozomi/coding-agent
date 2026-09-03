"""Production resource host around the shared AgentRunner state machine."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import threading

from nz_coder.mcp import MCPRuntime
from nz_coder.protocol.message_schema import is_synthetic_user_message
from nz_coder.protocol.public_error import to_public_error
from nz_coder.runtime.agent.agent_manager import (
    background_agent_manager,
    scoped_agent_message_sender,
    scoped_background_agent_manager,
)
from nz_coder.foundation.async_utils import to_thread_settled
from nz_coder.runtime.core.execution_context import (
    scoped_broad_test_guard,
    scoped_declared_test_scopes,
)
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.protocol.session_events import SessionEventBus, scoped_session_event_bus
from nz_coder.state.sessions import scoped_session
from nz_coder.tools import scoped_dynamic_tool_provider, scoped_dynamic_tools
from nz_coder.tools.question import scoped_question_asker
from nz_coder.tools.plan_mode import scoped_plan_mode_controller


class ProductionRuntimeHost:
    """Bind run-scoped resources, then enter the one shared AgentRunner."""

    async def run(self, agent, messages: list, on_tool=None, on_text=None,
                  on_token=None, stream: bool = True, execute=None) -> dict:
        if not callable(execute):
            raise TypeError("ProductionRuntimeHost requires an execution callback")
        if not hasattr(agent, "event_bus"):
            agent.event_bus = SessionEventBus(
                session_id=getattr(agent, "session_id", ""),
                run_id=getattr(agent, "trace_id", ""),
                agent_id=getattr(agent, "agent_id", ""),
            )
        if not hasattr(agent, "event_publisher"):
            agent.event_publisher = agent.event_bus.for_interaction(
                str(getattr(agent, "trace_id", "") or getattr(agent, "session_id", "session")),
                agent_invocation_id=str(getattr(agent, "agent_id", "") or ""),
            )
        agent._rotate_change_tracker_if_needed()
        agent.change_tracker.history_start = _last_user_message_position(messages)
        from nz_coder.state.memory import bind_memory_manager
        from nz_coder.mcp import scoped_mcp_runtime
        from nz_coder.state.skills import bind_skill_loader
        from nz_coder.foundation.workspace_trust import (
            current_config_snapshot,
            scoped_config_snapshot,
        )
        from nz_coder.runtime.agent.subagent import scoped_parent_context
        from nz_coder.tools.files import bind_tool_state

        if not hasattr(agent, "_mcp_runtime_lock"):
            agent._mcp_runtime_lock = threading.Lock()
            agent._mcp_runtime = None
        run_config_snapshot = getattr(agent, "config_snapshot", None)
        if run_config_snapshot is None:
            run_config_snapshot = current_config_snapshot(agent.workdir)
            agent.config_snapshot = run_config_snapshot
        with agent._mcp_runtime_lock:
            if agent._mcp_runtime is None:
                runtime_factory = getattr(agent, "_mcp_runtime_factory", MCPRuntime)
                agent._mcp_runtime = (
                    runtime_factory([], workspace=agent.workdir)
                    if getattr(agent, "tool_allowlist", None) is not None
                    else runtime_factory.configured(
                        workspace=agent.workdir,
                        config_snapshot=run_config_snapshot,
                    )
                )
                set_change_handler = getattr(agent._mcp_runtime, "set_change_handler", None)
                if callable(set_change_handler):
                    set_change_handler(agent._on_mcp_change)
            mcp_runtime = agent._mcp_runtime
        if not hasattr(agent, "background_agents"):
            agent.background_agents = background_agent_manager(agent.workdir, agent.session_id)
        agent.background_agents.bind_event_bus(agent.event_bus)
        agent.background_agents.bind_event_publisher(agent.event_publisher)
        if hasattr(agent, "lineage"):
            agent.background_agents.bind_lineage(agent.lineage)
        try:
            await to_thread_settled(mcp_runtime.start)
            with ExitStack() as stack:
                stack.enter_context(scoped_workdir(agent.workdir))
                stack.enter_context(scoped_config_snapshot(run_config_snapshot))
                stack.enter_context(scoped_broad_test_guard())
                stack.enter_context(scoped_declared_test_scopes())
                stack.enter_context(scoped_session(agent.session_id))
                stack.enter_context(scoped_session_event_bus(agent.event_publisher))
                stack.enter_context(scoped_background_agent_manager(agent.background_agents))
                stack.enter_context(scoped_agent_message_sender(
                    getattr(agent, "_background_message_manager", agent.background_agents),
                    getattr(agent, "_background_message_recipient", "worker"),
                ))
                stack.enter_context(bind_tool_state(txn=agent.txn, change_tracker=agent.change_tracker))
                stack.enter_context(bind_memory_manager(agent._mm))
                stack.enter_context(bind_skill_loader(agent._skill_loader))
                if getattr(agent, "agent_graph", None) is not None:
                    stack.enter_context(scoped_dynamic_tools([
                        agent.agent_graph.tool_definition(lambda: agent.current_agent_name)
                    ]))
                stack.enter_context(scoped_dynamic_tool_provider(mcp_runtime.tool_bindings))
                stack.enter_context(scoped_mcp_runtime(mcp_runtime))
                stack.enter_context(scoped_question_asker(getattr(agent, "question_asker", None)))
                from nz_coder.runtime.workflows.workflow_host import scoped_workflow_approval_asker

                stack.enter_context(scoped_workflow_approval_asker(
                    getattr(agent, "workflow_approval_asker", None)
                ))
                stack.enter_context(scoped_plan_mode_controller(getattr(agent, "plan_mode", None)))
                stack.enter_context(scoped_parent_context(
                    session_id=agent.session_id,
                    tracer=agent.tracer,
                    agent_id=agent.agent_id,
                    trace_id=agent.trace_id,
                    model_id=agent._active_model_id(),
                ))
                mcp_status = mcp_runtime.status_summary()
                if mcp_status:
                    agent.tracer.log(
                        "mcp_status",
                        servers=mcp_status,
                        tool_count=len(mcp_runtime.tool_bindings()),
                    )
                    agent._emit_session_event("session.mcp.status", {
                        "servers": mcp_status,
                        "tool_count": len(mcp_runtime.tool_bindings()),
                    })
                return await execute(agent, messages, on_tool, on_text, on_token, stream)
        except asyncio.CancelledError:
            agent._emit_session_event("session.run.cancelled", {})
            raise
        except Exception as exc:
            public_error = to_public_error(exc)
            agent._emit_session_event("session.run.failed", {
                "error_type": public_error.metadata.get("error_type", public_error.code),
                "error": public_error.to_dict(),
            })
            raise


def _last_user_message_position(messages: list) -> int:
    """Return the latest real user-message index for change history."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and not is_synthetic_user_message(message)
        ):
            return index
    return len(messages)
