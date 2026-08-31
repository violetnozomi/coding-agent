"""Tool policy behavior through run-scoped focused state."""
from __future__ import annotations

from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.core.tool_context import ToolPolicyContext
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy


class _Permissions:
    def ask_special(self, _kind, _metadata) -> bool:
        return False


class _RuntimeState:
    investigation_calls_since_edit = 0
    mutation_generation = 0
    strict_progress_blocks = 0

    def closure_phase_action(self, _tool_name, _tool_input=None):
        return "allow"

    def task_constraint_action(self, _tool_name, _tool_input=None):
        return "allow"


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


def _private_path_calls() -> list[dict]:
    """Return literal tool calls that must never reach a strict SWE checkout."""
    return [
        {
            "id": "read-private",
            "function": {
                "name": "read_file",
                "arguments": {"path": ".nz-coder/runs/raw.jsonl"},
            },
        },
        {
            "id": "list-private",
            "function": {
                "name": "list_directory",
                "arguments": {"path": ".nz-coder"},
            },
        },
        {
            "id": "grep-private",
            "function": {
                "name": "grep_search",
                "arguments": {"pattern": "token", "path": ".nz-coder-runs"},
            },
        },
        {
            "id": "glob-private",
            "function": {
                "name": "glob_search",
                "arguments": {"pattern": "**/.nz-coder/**", "path": "."},
            },
        },
        {
            "id": "bash-private",
            "function": {
                "name": "bash",
                "arguments": {
                    "command": "tail -c 3000 .nz-coder-runs/raw.jsonl",
                },
            },
        },
        {
            "id": "batch-write-private",
            "function": {
                "name": "write_files_batch",
                "arguments": {
                    "files": [{"path": "src/.nz-coder/cache", "content": "x"}],
                },
            },
        },
        {
            "id": "patch-private",
            "function": {
                "name": "apply_patch",
                "arguments": {
                    "changes": [{
                        "op": "create",
                        "path": ".nz-coder-runs/result.py",
                        "content": "x = 1\n",
                    }],
                },
            },
        },
    ]


def test_strict_private_path_policy_rejects_every_path_bearing_tool() -> None:
    """Dropping any path-field check would expose benchmark-private artifacts."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    calls = _private_path_calls()
    with scoped_runtime_overrides(strict_local_tools=True):
        rejected = ProductionToolPolicy().strict_private_path_rejections(
            _context(), calls,
        )

    assert list(rejected) == list(range(len(calls)))
    assert all(result.executed is False for result in rejected.values())
    assert all(result.dispatch_failed is True for result in rejected.values())
    assert all(result.permission_denied is True for result in rejected.values())
    assert all(
        result.metadata["guardrail"] == "strict_private_path"
        for result in rejected.values()
    )


def test_strict_private_path_policy_does_not_scan_search_or_file_content() -> None:
    """A private name used as source text is not a request to read that path."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    calls = [
        {
            "id": "grep-content",
            "function": {
                "name": "grep_search",
                "arguments": {"pattern": ".nz-coder", "path": "."},
            },
        },
        {
            "id": "bash-grep-content",
            "function": {
                "name": "bash",
                "arguments": {"command": "rg -n '.nz-coder' ."},
            },
        },
        {
            "id": "write-content",
            "function": {
                "name": "write_file",
                "arguments": {
                    "path": "docs/private-paths.md",
                    "content": "Never inspect .nz-coder-runs during evaluation.\n",
                },
            },
        },
    ]

    with scoped_runtime_overrides(strict_local_tools=True):
        rejected = ProductionToolPolicy().strict_private_path_rejections(
            _context(), calls,
        )

    assert rejected == {}


def test_private_paths_remain_available_outside_strict_swe_mode() -> None:
    """Ordinary product sessions may explicitly inspect NZ-Coder state."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    with scoped_runtime_overrides(strict_local_tools=False):
        rejected = ProductionToolPolicy().strict_private_path_rejections(
            _context(), _private_path_calls(),
        )

    assert rejected == {}


def test_strict_private_path_policy_tolerates_malformed_path_collections() -> None:
    """Malformed tool input must reach normal validation instead of crashing policy."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    calls = [
        {
            "id": "bad-batch",
            "function": {
                "name": "write_files_batch",
                "arguments": {"files": None},
            },
        },
        {
            "id": "bad-patch",
            "function": {
                "name": "apply_patch",
                "arguments": {"changes": None},
            },
        },
    ]

    with scoped_runtime_overrides(strict_local_tools=True):
        rejected = ProductionToolPolicy().strict_private_path_rejections(
            _context(), calls,
        )

    assert rejected == {}


def test_strict_private_paths_are_blocked_before_sync_tool_execution() -> None:
    """Removing the sync pipeline gate would execute this deliberately fatal host."""
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime

    class Guardrails:
        async def before_tool(self, _host, tool_call, _messages):
            return tool_call, None

        async def after_tool(self, _host, _tool_call, result, _messages):
            return result

    class Hooks:
        @staticmethod
        def has_pre_tool_use_hooks():
            return False

    class Host:
        runtime_services = SimpleNamespace(guardrails=Guardrails())
        hooks = Hooks()
        executor = object()

        @staticmethod
        def _execute_tool_call_with_hooks(_tool_call, _index, _messages):
            raise AssertionError("strict private tool call reached the executor")

    calls = _private_path_calls()
    with scoped_runtime_overrides(strict_local_tools=True):
        dispatched = ProductionToolRuntime().dispatch_sync(
            Host(), calls, has_write=True, messages=[], policy_context=_context(),
        )

    assert [result.metadata["guardrail"] for _, _, result in dispatched] == [
        "strict_private_path",
    ] * len(calls)


def test_strict_private_paths_are_blocked_before_async_tool_execution() -> None:
    """Removing the async pipeline gate would execute this deliberately fatal lifecycle."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime

    class Lifecycle:
        executor = object()

        @staticmethod
        async def before_tool(tool_call, _messages):
            return tool_call, None

        @staticmethod
        async def after_tool(_tool_call, result, _messages):
            return result

        @staticmethod
        def has_pre_tool_hooks():
            return False

        @staticmethod
        def execute_one(_tool_call, _index, _messages):
            raise AssertionError("strict private tool call reached the executor")

    context = _context()
    focused = SimpleNamespace(policy=context, lifecycle=Lifecycle())
    calls = _private_path_calls()
    with scoped_runtime_overrides(strict_local_tools=True):
        dispatched = asyncio.run(ProductionToolRuntime().dispatch_async(
            focused, calls, has_write=True, messages=[], policy_context=context,
        ))

    assert [result.metadata["guardrail"] for _, _, result in dispatched] == [
        "strict_private_path",
    ] * len(calls)


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


def test_runtime_owned_verification_is_excluded_from_model_stall_detection() -> None:
    """Runtime verification is not an assistant tool choice and cannot be a loop."""
    recorded = []
    traces = []

    class _Sidecar:
        def consume_pending_nudge(self):
            raise AssertionError("runtime-owned work must not consume model nudges")

        def record_tool_use(self, call):
            recorded.append(call)
            return True

    context = _context()
    context.stall_orchestrator = _Sidecar()
    context.trace = lambda event, **payload: traces.append((event, payload))
    calls = [{
        "id": "verification-contract-3-1",
        "function": {
            "name": "bash",
            "arguments": {
                "command": "python -m pytest -q tests",
                "_nz_runtime_contract": True,
            },
        },
    }]

    blocked = ProductionToolPolicy().find_repeated_tool_calls(context, calls)

    assert blocked == {}
    assert recorded == []
    assert context.recovery.repeated_tool_calls == 0
    assert context.recovery._last_tool_signature is None
    assert traces == [(
        "runtime_owned_stall_observation_skipped",
        {"name": "bash", "tool_call_id": "verification-contract-3-1"},
    )]


def test_model_verification_marker_does_not_bypass_stall_detection() -> None:
    """Projected/model-visible metadata is not proof of Runtime ownership."""
    recorded = []

    class _Sidecar:
        def consume_pending_nudge(self):
            return None

        def record_tool_use(self, call):
            recorded.append(call)
            return False

    context = _context()
    context.stall_orchestrator = _Sidecar()
    calls = [{
        "id": "call-model-probe",
        "function": {
            "name": "bash",
            "arguments": {
                "command": "python -c 'print(1)'",
                "_nz_runtime_verification_stage": "runtime",
            },
        },
    }]

    blocked = ProductionToolPolicy().find_repeated_tool_calls(context, calls)

    assert blocked == {}
    assert recorded == [{
        "id": "call-model-probe",
        "name": "bash",
        "input": {
            "command": "python -c 'print(1)'",
            "_nz_runtime_verification_stage": "runtime",
        },
    }]
    assert context.recovery.repeated_tool_calls == 1


def test_deterministic_closure_tools_skip_l2_but_keep_local_doom_guard() -> None:
    """Repeated evidence summaries need no paid judge, but must remain bounded."""
    recorded = []
    traces = []

    class _Sidecar:
        def consume_pending_nudge(self):
            return None

        def record_tool_use(self, call):
            recorded.append(call)
            return True

    context = _context()
    context.stall_orchestrator = _Sidecar()
    context.trace = lambda event, **payload: traces.append((event, payload))
    policy = ProductionToolPolicy()
    verify_call = {
        "id": "call-verify",
        "function": {"name": "verify_changed_files", "arguments": {}},
    }
    call = {
        "id": "call-diff",
        "function": {"name": "diff_status", "arguments": {}},
    }

    assert policy.find_repeated_tool_calls(context, [verify_call]) == {}
    assert policy.find_repeated_tool_calls(context, [call]) == {}
    assert policy.find_repeated_tool_calls(context, [call]) == {}
    blocked = policy.find_repeated_tool_calls(context, [call])

    assert recorded == []
    assert blocked[0].permission_denied is True
    assert blocked[0].metadata["stall_kind"] == "consecutive"
    assert [event for event, _payload in traces].count(
        "stall_sidecar_observation_skipped"
    ) == 4


def test_product_phase_policy_allows_new_read_after_localization() -> None:
    """Source/test localization must not turn a distinct read into a rejection."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    context = _context()
    state = RuntimeState()
    state.task_mode = "bugfix"
    state.read_files = ["src/parser.py", "tests/test_parser.py"]
    state.investigation_calls_since_edit = 6
    context.runtime_state = state
    call = {
        "id": "call-late-read",
        "function": {
            "name": "read_file",
            "arguments": {"path": "src/unrelated.py"},
        },
    }

    rejected = ProductionToolPolicy().implementation_phase_rejections(
        context, [call],
    )

    assert rejected == {}


def test_task_constraint_gate_blocks_explicitly_forbidden_test_mutation() -> None:
    context = _context()
    context.runtime_state.task_constraint_action = (
        lambda name, tool_input=None: (
            "block"
            if name == "apply_patch"
            and tool_input.get("path") == "tests/test_app.py"
            else "allow"
        )
    )
    calls = [
        {"id": "test-edit", "function": {
            "name": "apply_patch",
            "arguments": {"path": "tests/test_app.py", "patch": "..."},
        }},
        {"id": "source-edit", "function": {
            "name": "edit_file",
            "arguments": {"path": "app.py", "old_text": "a", "new_text": "b"},
        }},
    ]

    rejected = ProductionToolPolicy().task_constraint_rejections(context, calls)

    assert list(rejected) == [0]
    assert rejected[0].metadata["guardrail"] == "task_constraint"
    assert "forbids modifying test files" in rejected[0].output


def test_nominal_closure_policy_does_not_gate_worker_investigation() -> None:
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    context = _context()
    state = RuntimeState()
    state.work_phase = "closure_repair"
    context.runtime_state = state
    calls = [
        {"id": "broad", "function": {"name": "repo_map", "arguments": {}}},
        {"id": "search", "function": {
            "name": "grep_search", "arguments": {"pattern": "clue", "path": "."},
        }},
        {"id": "edit", "function": {
            "name": "apply_patch",
            "arguments": {"path": "app.py", "old_text": "a", "new_text": "b"},
        }},
    ]

    rejected = ProductionToolPolicy().closure_phase_rejections(context, calls)

    assert rejected == {}


def test_nominal_closure_policy_keeps_compound_git_write_safety_boundary() -> None:
    """Removing convergence gates must not authorize a hidden Git mutation."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    context = _context()
    state = RuntimeState()
    state.work_phase = "closure_finalize"
    state.workspace_git_available = True
    context.runtime_state = state
    calls = [{
        "id": "compound-git",
        "function": {
            "name": "bash",
            "arguments": {"command": "git diff && git checkout -- app.py"},
        },
    }]

    rejected = ProductionToolPolicy().closure_phase_rejections(context, calls)

    assert list(rejected) == [0]
    assert rejected[0].metadata["guardrail"] == "shell_write_boundary"
    assert "compound or mutating Git observation" in rejected[0].output


def test_legacy_bounded_emergency_state_does_not_close_exploration() -> None:
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    context = _context()
    state = RuntimeState()
    state.work_phase = "bounded_emergency"
    state.has_diff = True
    state.changed_files = ["app.py"]
    state.verification_failures = 1
    context.runtime_state = state
    calls = [
        {"id": "broad", "function": {
            "name": "repo_map", "arguments": {"path": "."},
        }},
        {"id": "edit", "function": {
            "name": "edit_file", "arguments": {"path": "app.py"},
        }},
    ]

    rejected = ProductionToolPolicy().closure_phase_rejections(context, calls)

    assert rejected == {}
    assert state.emergency_broad_exploration == 0


def test_closure_gate_explains_non_git_convergence_and_traces_reason() -> None:
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    traces = []
    context = _context()
    context.trace = lambda event, **payload: traces.append((event, payload))
    state = RuntimeState()
    state.work_phase = "closure_repair"
    state.workspace_git_available = False
    context.runtime_state = state
    calls = [{
        "id": "git-diff",
        "function": {
            "name": "bash",
            "arguments": {"command": "git diff -- app.py"},
        },
    }]

    rejected = ProductionToolPolicy().closure_phase_rejections(context, calls)

    assert rejected[0].metadata["reason"] == "git_required_but_unavailable"
    assert "diff_status" in rejected[0].output
    assert traces[-1][1]["reason"] == "git_required_but_unavailable"
