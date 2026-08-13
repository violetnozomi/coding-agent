"""Tool policy behavior through run-scoped focused state."""
from __future__ import annotations

from nz_coder.recovery import RecoveryState
from nz_coder.runtime.core.tool_context import ToolPolicyContext
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy


class _Permissions:
    def ask_special(self, _kind, _metadata) -> bool:
        return False


class _RuntimeState:
    investigation_calls_since_edit = 0
    mutation_generation = 0
    strict_progress_blocks = 0


def _context(*, allowlist=None) -> ToolPolicyContext:
    return ToolPolicyContext(
        agent_name="reviewer",
        agent_graph=None,
        tool_allowlist=(
            frozenset(allowlist) if allowlist is not None else None
        ),
        admission_handle=None,
        runtime_state=_RuntimeState(),
        recovery=RecoveryState(),
        permissions=_Permissions(),
        stall_orchestrator=None,
        parse_input=lambda value: value if isinstance(value, dict) else {},
        trace=lambda _event, **_payload: None,
    )


def test_focused_policy_denies_tool_outside_agent_allowlist() -> None:
    """Reading host.tool_allowlist instead of context state would break isolation."""
    context = _context(allowlist={"read_file"})
    call = {
        "id": "call-write",
        "function": {"name": "write_file", "arguments": {"path": "a.py"}},
    }

    rejected = ProductionToolPolicy().agent_tool_rejections(context, [call])

    assert list(rejected) == [0]
    assert rejected[0].permission_denied is True
    assert "reviewer" in rejected[0].output
    assert "write_file" in rejected[0].output


def test_focused_policy_batch_identity_and_observability_are_run_scoped() -> None:
    """Mutating AgentLoop batch fields would leave focused state unchanged."""
    context = _context()
    policy = ProductionToolPolicy()
    calls = [{"function": {"name": "read_file", "arguments": {}}}]

    first, _started = policy.begin_tool_batch(context, calls, False)
    second, _started = policy.begin_tool_batch(context, calls, False)
    policy.finish_tool_batch_observation(
        context,
        batch_id=second,
        started=_started,
        mode="single",
        dispatched=[],
        segments=[],
    )

    assert (first, second) == ("batch-1", "batch-2")
    assert context.observability["batches"] == 1
