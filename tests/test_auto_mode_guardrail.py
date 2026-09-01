"""Production integration for the interactive Auto-mode guardrail."""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.interface import cli
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.agent.auto_mode import AutoAdmission
from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.execution.tool_executor import ToolExecutionResult
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime


class _GuardrailHost:
    def __init__(self, *, agent_graph, controller) -> None:
        self.agent_graph = agent_graph
        self.auto_mode_controller = controller
        self.current_agent_name = "worker"
        self.tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)

    def _auto_mode_context(self):
        return object()


class _FixedController:
    def __init__(self, admission: AutoAdmission) -> None:
        self.admission = admission

    async def admit(self, _context, _tool_name, _tool_input, _messages):
        return self.admission


def _rewriting_tool_batch():
    """Return one approved rewrite and the private source value."""
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    raw_secret = "raw-tool-secret"

    async def rewrite(call, _context):
        selected = {
            **call,
            "function": {
                **call["function"],
                "arguments": {"path": "safe.py"},
            },
        }
        return {"action": "rewrite", "payload": selected}

    host = _GuardrailHost(
        agent_graph=AgentGraph([
            AgentSpec(
                "worker",
                "worker",
                guardrails=(ToolGuardrail("rewrite", before_tool=rewrite),),
            ),
        ], start="worker"),
        controller=None,
    )
    host.runtime_services = SimpleNamespace(
        guardrails=ProductionGuardrailRuntime(),
    )
    batch = ProductionToolRuntime().approve_tool_calls_sync(
        host,
        [{
            "id": "call-rewrite",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": {"path": raw_secret},
            },
        }],
        [],
    )
    return batch, raw_secret


def test_raw_tool_arguments_not_persisted_before_guardrail():
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    batch, raw_secret = _rewriting_tool_batch()
    message = {"role": "assistant", "content": ""}
    attach_message_identity(message, session_id="tool-boundary")
    processor = SessionProcessor(message)
    processor.register_tool_calls(batch.calls)

    assert raw_secret not in repr(message)


def test_tool_guardrail_rewrite_is_the_only_persisted_input():
    from nz_coder.protocol.message_schema import PARTS_KEY, attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    batch, _raw_secret = _rewriting_tool_batch()
    message = {"role": "assistant", "content": ""}
    attach_message_identity(message, session_id="tool-rewrite")
    processor = SessionProcessor(message)
    processor.register_tool_calls(batch.calls)

    part = next(item for item in message[PARTS_KEY] if item["type"] == "tool")
    assert part["state"]["input"] == {"path": "safe.py"}
    assert batch.calls[0]["function"]["arguments"] == {"path": "safe.py"}


def test_tool_guardrail_rewrite_matches_executed_input():
    batch, _raw_secret = _rewriting_tool_batch()
    executed = dict(batch.calls[0]["function"]["arguments"])

    assert executed == {"path": "safe.py"}


async def _approve_once(_name, _tool_input, _details):
    return "once"


def test_product_environment_auto_classifier_defaults_off() -> None:
    """Non-terminal constructors cannot silently gain classifier calls."""
    parameter = inspect.signature(ProductRunEnvironment.__init__).parameters[
        "auto_mode_classifier_enabled"
    ]

    assert parameter.default is False


@pytest.mark.parametrize("value", ["false", 0, None])
def test_product_environment_rejects_non_boolean_auto_capability(value) -> None:
    """Configuration-like values cannot become truthy through coercion."""
    with pytest.raises(TypeError, match="must be a bool"):
        ProductRunEnvironment("system", auto_mode_classifier_enabled=value)


def test_auto_mode_context_requires_capability_mode_and_async_approval(
    tmp_path,
) -> None:
    """All three local-interactive gates must hold before building a Gateway."""
    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    environment.auto_mode_controller = SimpleNamespace(enabled=False)
    environment.permissions = SimpleNamespace(mode="auto")
    environment.auto_permission_asker = _approve_once
    environment.workdir = tmp_path
    environment.tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)
    environment._gateway = lambda **_kwargs: SimpleNamespace(complete=object())

    assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.auto_mode_controller.enabled = True
    for mode in ("default", "plan", "acceptEdits"):
        environment.permissions.mode = mode
        assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.permissions.mode = "auto"
    environment.auto_permission_asker = None
    assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.auto_permission_asker = lambda *_args: "once"
    assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.auto_permission_asker = _approve_once
    assert ProductRunEnvironment._auto_mode_context(environment) is not None


def test_cli_build_agent_enables_auto_classifier_at_terminal_composition(
    monkeypatch,
) -> None:
    """The local CLI is the sole default-on composition point."""
    captured = {}
    sentinel = object()
    monkeypatch.setattr(
        "nz_coder.providers.models.active_model_selection",
        lambda: SimpleNamespace(provider="fake"),
    )
    monkeypatch.setattr(
        "nz_coder.providers.configuration.provider_connection",
        lambda _provider: SimpleNamespace(provider="fake", configured=True),
    )

    def build(_system_prompt, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "nz_coder.runtime.execution.composition.build_product_environment",
        build,
    )

    result = cli._build_agent("system", object(), "session-a")

    assert result is sentinel
    assert captured["auto_mode_classifier_enabled"] is (
        config.AUTO_MODE_CLASSIFIER_ENABLED
    )


def test_guardrail_runs_session_auto_policy_without_agent_graph() -> None:
    """The default coding runtime has no graph but still owns Auto admission."""
    calls = []

    class Controller:
        async def admit(self, context, tool_name, tool_input, messages):
            calls.append((context, tool_name, tool_input, messages))
            return AutoAdmission(
                allowed=True,
                permission_denied=False,
                source="classifier",
                reason="safe",
                reason_code="safe",
                action_digest="a" * 64,
                classifier_status="completed",
            )

    host = _GuardrailHost(agent_graph=None, controller=Controller())
    guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {
            "id": "call-1",
            "function": {
                "name": "bash",
                "arguments": '{"command":"git status"}',
            },
        },
        [{"role": "user", "content": "inspect"}],
    ))

    assert rejected is None
    assert guarded["id"] == "call-1"
    assert calls[0][1:3] == ("bash", {"command": "git status"})


def test_guardrail_projects_user_reject_as_permission_denied_result() -> None:
    """A human rejection remains model-recoverable and never dispatches."""
    admission = AutoAdmission(
        allowed=False,
        permission_denied=True,
        source="human",
        reason="Rejected by user",
        reason_code="user_reject",
        action_digest="b" * 64,
        classifier_status="completed",
    )
    host = _GuardrailHost(
        agent_graph=None,
        controller=_FixedController(admission),
    )

    _guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {
            "id": "call-2",
            "function": {
                "name": "bash",
                "arguments": {"command": "python build.py"},
            },
        },
        [],
    ))

    assert rejected.executed is False
    assert rejected.dispatch_failed is True
    assert rejected.permission_denied is True
    assert rejected.output.startswith("Denied:")
    assert rejected.metadata["guardrail"] == "auto_mode"


def test_guardrail_skips_auto_for_malformed_tool_arguments() -> None:
    """The stable executor remains the owner of invalid-JSON diagnostics."""
    class Controller:
        async def admit(self, *_args):
            raise AssertionError("malformed arguments reached Auto admission")

    call = {
        "id": "call-invalid",
        "function": {"name": "bash", "arguments": "{"},
    }
    guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        _GuardrailHost(agent_graph=None, controller=Controller()),
        call,
        [],
    ))

    assert guarded == call
    assert rejected is None


def test_guardrail_skips_auto_for_nonfinite_tool_arguments() -> None:
    """Non-finite values stay on the executor's invalid-argument path."""
    calls = []

    class Controller:
        async def admit(self, *_args):
            calls.append("auto")
            raise AssertionError("non-finite arguments reached Auto admission")

    host = _GuardrailHost(agent_graph=None, controller=Controller())
    tool_calls = [
        {
            "id": "call-nan-json",
            "function": {"name": "bash", "arguments": '{"command":NaN}'},
        },
        {
            "id": "call-nan-dict",
            "function": {
                "name": "bash",
                "arguments": {"command": float("nan")},
            },
        },
    ]

    for tool_call in tool_calls:
        _guarded, rejected = asyncio.run(
            ProductionGuardrailRuntime().before_tool(host, tool_call, [])
        )
        assert rejected is None
    assert calls == []


def test_auto_admission_receives_declared_guardrail_rewrite() -> None:
    """Classifier scope is computed from the final guarded arguments."""
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    observed = []

    def rewrite(call, _context):
        rewritten = dict(call)
        rewritten["function"] = {
            "name": "bash",
            "arguments": {"command": "python build.py"},
        }
        return {"action": "rewrite", "payload": rewritten}

    class Controller:
        async def admit(self, _context, name, tool_input, _messages):
            observed.append((name, tool_input))
            return AutoAdmission(
                allowed=True,
                permission_denied=False,
                source="classifier",
                reason="safe",
                reason_code="safe",
                action_digest="d" * 64,
                classifier_status="completed",
            )

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER_ROLE",
            guardrails=(ToolGuardrail("rewrite", before_tool=rewrite),),
        ),
    ], start="worker")
    host = _GuardrailHost(agent_graph=graph, controller=Controller())
    guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {
            "id": "call-rewrite",
            "function": {"name": "bash", "arguments": {"command": "git status"}},
        },
        [],
    ))

    assert rejected is None
    assert guarded["function"]["arguments"] == {"command": "python build.py"}
    assert observed == [("bash", {"command": "python build.py"})]


def test_sync_dispatch_never_invokes_async_auto_admission() -> None:
    """Compatibility dispatch cannot bind async terminal approval to a fresh loop."""
    calls = []

    class Controller:
        async def admit(self, *_args):
            calls.append("auto")
            return AutoAdmission(
                allowed=True,
                permission_denied=False,
                source="classifier",
                reason="safe",
                reason_code="safe",
                action_digest="c" * 64,
                classifier_status="completed",
            )

    class Hooks:
        @staticmethod
        def has_pre_tool_use_hooks():
            return False

    class Host(_GuardrailHost):
        def __init__(self):
            super().__init__(agent_graph=None, controller=Controller())
            self.runtime_services = SimpleNamespace(
                guardrails=ProductionGuardrailRuntime(),
            )
            self.permissions = PermissionManager("auto")
            self.recovery = RecoveryState()
            self.hooks = Hooks()
            self.executor = object()

        @staticmethod
        def _best_effort_tool_input(raw):
            return dict(raw) if isinstance(raw, dict) else {}

        @staticmethod
        def _execute_tool_call_with_hooks(tool_call, _index, _messages):
            name = tool_call["function"]["name"]
            tool_input = dict(tool_call["function"].get("arguments") or {})
            return ToolExecutionResult(
                name=name,
                tool_input=tool_input,
                output="ok",
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            )

    dispatched = ProductionToolRuntime().dispatch_sync(
        Host(),
        [{
            "id": "sync-call",
            "function": {"name": "read_file", "arguments": {"path": "app.py"}},
        }],
        has_write=False,
        messages=[],
    )

    assert calls == []
    assert dispatched[0][2].output == "ok"


def test_auto_admission_settles_batch_serially_before_scheduling() -> None:
    """Two residual calls are admitted once each before tool workers run."""
    from nz_coder.runtime.core.tool_context import (
        ToolExecutionContext,
        ToolLifecycleContext,
        ToolPolicyContext,
        ToolProjectionContext,
    )

    async def scenario():
        active = 0
        max_active = 0
        admitted = []
        executed = []

        class Controller:
            async def admit(self, _context, name, tool_input, _messages):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                admitted.append((name, tool_input["command"]))
                await asyncio.sleep(0)
                active -= 1
                return AutoAdmission(
                    allowed=True,
                    permission_denied=False,
                    source="classifier",
                    reason="safe",
                    reason_code="safe",
                    action_digest=str(len(admitted)) * 64,
                    classifier_status="completed",
                )

        class Executor:
            @staticmethod
            def execute_one(tool_call, _index):
                command = tool_call["function"]["arguments"]["command"]
                executed.append(command)
                return ToolExecutionResult(
                    name="bash",
                    tool_input={"command": command},
                    output="ok",
                    executed=True,
                    dispatch_failed=False,
                    command_failed=False,
                    is_write=False,
                )

        class Observer:
            post_write = staticmethod(lambda *_args: None)
            after_batch = staticmethod(lambda *_args: None)
            apply_plan_mode = staticmethod(lambda *_args: None)
            capture_snapshot = staticmethod(lambda *_args: None)
            record_patch = staticmethod(lambda *_args: None)

        async def noop_async(*_args):
            return None

        host = _GuardrailHost(agent_graph=None, controller=Controller())
        guardrails = ProductionGuardrailRuntime()
        executor = Executor()

        async def before_tool(tool_call, messages):
            return await guardrails.before_tool(host, tool_call, messages)

        async def after_tool(_tool_call, result, _messages):
            return result

        policy = ToolPolicyContext(
            agent_name="worker",
            agent_graph=None,
            tool_allowlist=None,
            admission_handle=None,
            runtime_state=None,
            recovery=RecoveryState(),
            permissions=PermissionManager("auto"),
            stall_orchestrator=None,
            parse_input=lambda raw: dict(raw) if isinstance(raw, dict) else {},
            trace=lambda *_args, **_kwargs: None,
        )
        lifecycle = ToolLifecycleContext(
            checkpoint=noop_async,
            processor_for_messages=lambda _messages: None,
            write_override=None,
            begin_transaction=lambda: None,
            transaction_active=lambda: False,
            finish_transaction=lambda *_args: None,
            metadata_reporter=lambda *_args: None,
            question_reporter=lambda *_args: None,
            dispatch_override_async=None,
            consume_override=None,
            model_capabilities=None,
            describe_read_results=noop_async,
            strict_completed=lambda _results: False,
            apply_transition=noop_async,
            observer=Observer(),
            has_pre_tool_hooks=lambda: False,
            executor=executor,
            execute_one=executor.execute_one,
            before_tool=before_tool,
            after_tool=after_tool,
            trace=lambda *_args, **_kwargs: None,
        )
        projection = ToolProjectionContext(
            signal_from_metadata=lambda _metadata: None,
            record_result=lambda _result: False,
            trace_result=lambda *_args, **_kwargs: None,
            stall_orchestrator=None,
            after_result=lambda *_args: None,
        )
        calls = [
            {
                "id": "batch-1",
                "function": {"name": "bash", "arguments": {"command": "git status"}},
            },
            {
                "id": "batch-2",
                "function": {"name": "bash", "arguments": {"command": "git diff"}},
            },
        ]
        dispatched = await ProductionToolRuntime().dispatch_async(
            ToolExecutionContext(
                run=None,
                policy=policy,
                lifecycle=lifecycle,
                projection=projection,
            ),
            calls,
            has_write=False,
            messages=[],
        )
        return max_active, admitted, executed, dispatched

    max_active, admitted, executed, dispatched = asyncio.run(scenario())

    assert max_active == 1
    assert admitted == [("bash", "git status"), ("bash", "git diff")]
    assert sorted(executed) == ["git diff", "git status"]
    assert len(dispatched) == 2
