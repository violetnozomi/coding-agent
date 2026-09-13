"""Regression tests for context-local agent runtime state."""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock, get_ident
from types import MethodType

from nz_coder.foundation import config
from nz_coder.foundation.user_paths import user_storage_layout
from nz_coder.state.memory import MemoryManager, bind_memory_manager, current_memory_manager
from nz_coder.foundation.async_utils import start_background_coro
from nz_coder.runtime.execution.loop import (
    AgentLoop,
    _execute_concurrent,
    _execute_scheduled,
    _execute_scheduled_async,
)
from nz_coder.runtime.process.workdir import current_derived_path, current_workdir, scoped_workdir
from nz_coder.runtime.execution.tool_executor import is_write_tool
from nz_coder.state.sessions import active_session_id
from nz_coder.state.skills import SkillLoader, current_skill_loader
from nz_coder.runtime.agent.subagent import _PARENT_CONTEXT
from nz_coder.tools import (
    TOOL_EXECUTION_MODES,
    TOOL_HANDLERS,
    TOOL_SIDE_EFFECTS,
    TOOL_SPECS,
    get_execution_mode,
    register,
)
from nz_coder.tools.files import _get_change_tracker, _get_txn, bind_tool_state, write_file
from nz_coder.tools.scratchpad import scratchpad
from nz_coder.state.transaction import TransactionManager


class _Tracker:
    def __init__(self):
        self.paths = []

    def record_before(self, path, exists, content):
        self.paths.append(("before", path))

    def record_after(self, path, exists, content):
        self.paths.append(("after", path))


def test_workspace_and_tool_state_are_isolated_across_threads(tmp_path):
    original = current_workdir()
    barrier = Barrier(2)

    def worker(name):
        root = tmp_path / name
        root.mkdir()
        txn = TransactionManager()
        tracker = _Tracker()
        with scoped_workdir(root), bind_tool_state(txn=txn, change_tracker=tracker):
            barrier.wait()
            assert current_workdir() == root.resolve()
            assert current_derived_path("SESSION_DIR") == (
                user_storage_layout(root).workspace_state / "sessions"
            )
            assert _get_txn() is txn
            assert _get_change_tracker() is tracker
            result = write_file("shared.txt", name)
            barrier.wait()
        return root, result, tracker.paths

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, name) for name in ("alpha", "beta")]
        results = [future.result() for future in futures]

    assert current_workdir() == original
    assert config.WORKDIR.resolve() == original
    for root, result, paths in results:
        assert not result.startswith("Error:")
        assert (root / "shared.txt").read_text(encoding="utf-8") == root.name
        assert paths == [("before", "shared.txt"), ("after", "shared.txt")]


class _Executor:
    def execute_one(self, tool_call, index):
        return str(current_workdir())


def test_sync_concurrent_tools_inherit_workspace_context(tmp_path):
    root = tmp_path / "agent"
    calls = [{"function": {"name": "probe"}} for _ in range(2)]
    with scoped_workdir(root):
        results = _execute_concurrent(_Executor(), calls)
    assert [item[2] for item in results] == [str(root.resolve())] * 2


def test_nested_scoped_workdir_restores_parent_context(tmp_path):
    original = current_workdir()
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    with scoped_workdir(outer):
        assert current_workdir() == outer.resolve()
        with scoped_workdir(inner):
            assert current_workdir() == inner.resolve()
        assert current_workdir() == outer.resolve()
    assert current_workdir() == original


def test_pre_edit_contract_keeps_investigation_tools_visible(monkeypatch):
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.task_mode = "feature"
    state.verification_contract = {"command": "pytest tests/test_app.py"}
    state.investigation_calls_since_edit = 6
    agent = AgentLoop.__new__(AgentLoop)
    agent.runtime_state = state
    agent._structured_output_active_repair = ""
    agent.repo_intelligence = SimpleNamespace(semantic_available=True)
    agent.tool_allowlist = None
    agent.agent_graph = None
    agent.provider_id = "openai-compatible"
    agent.model_id = "test-model"
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.get_specs",
        lambda: [
            {"type": "function", "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            }}
            for name in ("read_file", "repo_map", "edit_file", "bash", "todo")
        ],
    )

    names = {
        item["function"]["name"] for item in agent._active_tool_specs()
    }

    assert {"read_file", "repo_map", "edit_file", "bash", "todo"} <= names


def test_localized_pre_edit_scope_keeps_investigation_without_contract(monkeypatch):
    """Localization is evidence, not authority to withdraw distinct tools."""
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.task_mode = "bugfix"
    state.read_files = ["src/parser.py", "tests/test_parser.py"]
    state.investigation_calls_since_edit = 6
    agent = AgentLoop.__new__(AgentLoop)
    agent.runtime_state = state
    agent._structured_output_active_repair = ""
    agent.repo_intelligence = SimpleNamespace(semantic_available=True)
    agent.tool_allowlist = None
    agent.agent_graph = None
    agent.provider_id = "openai-compatible"
    agent.model_id = "test-model"
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.get_specs",
        lambda: [
            {"type": "function", "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            }}
            for name in ("read_file", "repo_map", "edit_file", "bash", "todo")
        ],
    )

    names = {
        item["function"]["name"] for item in agent._active_tool_specs()
    }

    assert {"read_file", "repo_map", "edit_file", "bash", "todo"} <= names


def test_nominal_closure_keeps_full_worker_tool_surface(monkeypatch):
    """Budget convergence must not hide investigation or dispatch schemas."""
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    agent = AgentLoop.__new__(AgentLoop)
    agent.runtime_state = state
    agent._structured_output_active_repair = ""
    agent.repo_intelligence = SimpleNamespace(semantic_available=True)
    agent.tool_allowlist = None
    agent.agent_graph = None
    agent.provider_id = "openai-compatible"
    agent.model_id = "test-model"
    expected = {"read_file", "grep_search", "repo_map", "task", "edit_file", "bash"}
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.get_specs",
        lambda: [
            {"type": "function", "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            }}
            for name in expected
        ],
    )

    for phase in ("closure_repair", "closure_finalize"):
        state.work_phase = phase
        names = {item["function"]["name"] for item in agent._active_tool_specs()}

        assert expected <= names


def test_contract_led_task_keeps_bash_schema_before_first_mutation(monkeypatch):
    """A declared check must not hide the shell needed to inspect its harness."""
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.task_mode = "feature"
    state.verification_contract = {"command": "pytest tests/test_app.py"}
    agent = AgentLoop.__new__(AgentLoop)
    agent.runtime_state = state
    agent._structured_output_active_repair = ""
    agent.repo_intelligence = SimpleNamespace(semantic_available=True)
    agent.tool_allowlist = None
    agent.agent_graph = None
    agent.provider_id = "openai-compatible"
    agent.model_id = "test-model"
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.get_specs",
        lambda: [
            {"type": "function", "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            }}
            for name in ("read_file", "edit_file", "bash")
        ],
    )

    before_edit = {
        item["function"]["name"] for item in agent._active_tool_specs()
    }
    state.mutation_generation = 1
    after_edit = {
        item["function"]["name"] for item in agent._active_tool_specs()
    }

    assert "bash" in before_edit
    assert "bash" in after_edit


def test_strict_bash_schema_does_not_advertise_package_installation(monkeypatch):
    """Provider-facing tool guidance must agree with strict runtime authority."""
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    agent = AgentLoop.__new__(AgentLoop)
    agent.runtime_state = RuntimeState()
    agent._structured_output_active_repair = ""
    agent.repo_intelligence = SimpleNamespace(semantic_available=True)
    agent.tool_allowlist = None
    agent.agent_graph = None
    agent.provider_id = "openai-compatible"
    agent.model_id = "test-model"
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.get_specs",
        lambda: [{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run tests or install packages.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        strict_description = agent._active_tool_specs()[0]["function"][
            "description"
        ]
    normal_description = agent._active_tool_specs()[0]["function"]["description"]

    assert "installation is forbidden" in strict_description
    assert "direct narrow pytest" in strict_description
    assert normal_description == "Run tests or install packages."


def test_agent_loops_bind_their_own_runtime_context(tmp_path):
    async def scenario():
        agents = []
        expected = []
        for name in ("alpha", "beta"):
            root = tmp_path / name
            root.mkdir()
            txn = TransactionManager()
            tracker = _Tracker()
            agent = AgentLoop.__new__(AgentLoop)
            agent.workdir = root
            agent.txn = txn
            agent.change_tracker = tracker
            agent.session_id = name
            agent._mm = MemoryManager(root / ".nz-coder" / "memory")
            agent._skill_loader = SkillLoader(
                bundled_dir=root / "bundled-skills",
                user_dir=root / "user-skills",
                project_dir=root / ".nz-coder" / "skills",
            )
            agent._sp = scratchpad
            agent.tracer = tracker
            agent.agent_id = f"agent-{name}"
            agent.trace_id = f"trace-{name}"

            async def fake_run(self, messages, on_tool, on_text, on_token, stream):
                self._sp.clear()
                self._sp.update("plan", f"plan-{self.session_id}")
                await asyncio.sleep(0)
                parent = _PARENT_CONTEXT.get()
                context = (
                    current_workdir(),
                    _get_txn(),
                    _get_change_tracker(),
                    active_session_id(),
                    current_memory_manager(),
                    current_skill_loader(),
                    parent.get("session_id"),
                    parent.get("agent_id"),
                )
                return context, self._sp.read()

            agent._run = MethodType(fake_run, agent)
            agents.append(agent)
            expected_context = (
                root.resolve(),
                txn,
                tracker,
                name,
                agent._mm,
                agent._skill_loader,
                name,
                f"agent-{name}",
            )
            expected.append((expected_context, name))

        results = await asyncio.gather(*(agent.run([]) for agent in agents))
        return results, expected

    results, expected = asyncio.run(scenario())
    for (context, scratch), (expected_context, name) in zip(results, expected):
        assert context == expected_context
        assert f"plan-{name}" in scratch


class _CountingExecutor:
    def __init__(self, delay=0.03):
        self.delay = delay
        self.lock = Lock()
        self.active = 0
        self.max_active = 0

    def execute_one(self, tool_call, index):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            return index
        finally:
            with self.lock:
                self.active -= 1


class _NoToolHooks:
    def has_pre_tool_use_hooks(self):
        return False


def _task_call(agent_type, index):
    return {
        "id": f"task-{index}",
        "function": {"name": "task", "arguments": {"agent_type": agent_type}},
    }


def test_read_only_tasks_parallelize_but_general_tasks_remain_serial():
    old_limit = config.MAX_PARALLEL_TASKS
    config.MAX_PARALLEL_TASKS = 2
    try:
        agent = AgentLoop.__new__(AgentLoop)
        agent.hooks = _NoToolHooks()

        def run_batch(agent_types):
            executor = _CountingExecutor()
            agent.executor = executor

            def execute_with_hooks(self, tool_call, index, messages):
                return self.executor.execute_one(tool_call, index)

            agent._execute_tool_call_with_hooks = MethodType(execute_with_hooks, agent)
            calls = [
                _task_call(agent_type, i) for i, agent_type in enumerate(agent_types)
            ]
            results = asyncio.run(agent._dispatch_tool_calls_async(calls, False, []))
            return executor.max_active, [item[2] for item in results]

        read_max, read_order = run_batch(["explore", "plan", "reflection"])
        general_max, general_order = run_batch(["general-purpose", "general-purpose"])
        assert read_max == 2
        assert read_order == [0, 1, 2]
        assert general_max == 1
        assert general_order == [0, 1]
    finally:
        config.MAX_PARALLEL_TASKS = old_limit


def test_background_coroutine_inherits_memory_context(tmp_path):
    manager = MemoryManager(tmp_path / "memory")
    observed = []

    async def capture_manager():
        observed.append(current_memory_manager())

    with bind_memory_manager(manager):
        thread = start_background_coro(capture_manager())
    thread.join(timeout=2)
    assert observed == [manager]


class _BarrierExecutor:
    def __init__(self):
        self.lock = Lock()
        self.active = 0
        self.max_active = 0
        self.events = []

    def execute_one(self, tool_call, index):
        name = tool_call["function"]["name"]
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.append(("start", index, name, time.monotonic()))
        try:
            time.sleep(0.02 if name == "read" else 0.005)
            return index
        finally:
            with self.lock:
                self.events.append(("end", index, name, time.monotonic()))
                self.active -= 1


def _mixed_calls():
    names = ["read", "read", "serial", "read", "read"]
    return [
        {"id": str(index), "function": {"name": name, "arguments": {}}}
        for index, name in enumerate(names)
    ]


def _assert_barrier_schedule(executor, results):
    assert [item[0] for item in results] == [0, 1, 2, 3, 4]
    assert [item[2] for item in results] == [0, 1, 2, 3, 4]
    assert executor.max_active == 2
    timestamps = {
        (event, index): timestamp
        for event, index, _name, timestamp in executor.events
    }
    assert timestamps[("start", 2)] >= max(
        timestamps[("end", 0)], timestamps[("end", 1)],
    )
    assert min(
        timestamps[("start", 3)], timestamps[("start", 4)],
    ) >= timestamps[("end", 2)]


def test_mixed_tool_batch_parallelizes_read_segments_around_serial_barrier():
    def predicate(call):
        return call["function"]["name"] == "read"

    sync_executor = _BarrierExecutor()
    sync_results = _execute_scheduled(sync_executor, _mixed_calls(), predicate)
    _assert_barrier_schedule(sync_executor, sync_results)

    async_executor = _BarrierExecutor()
    async_results = asyncio.run(
        _execute_scheduled_async(async_executor, _mixed_calls(), predicate)
    )
    _assert_barrier_schedule(async_executor, async_results)


def test_async_serial_barrier_runs_blocking_tool_off_event_loop_thread():
    class Executor:
        def __init__(self):
            self.thread_id = None

        def execute_one(self, _tool_call, _index):
            self.thread_id = get_ident()
            return "done"

    async def scenario():
        executor = Executor()
        owner_thread = get_ident()
        results = await _execute_scheduled_async(
            executor,
            [{"id": "one", "function": {"name": "serial", "arguments": {}}}],
            lambda _call: False,
        )
        return executor, owner_thread, results

    executor, owner_thread, results = asyncio.run(scenario())

    assert executor.thread_id != owner_thread
    assert results[0][2] == "done"


def test_tool_concurrency_requires_explicit_read_effect():
    agent = AgentLoop.__new__(AgentLoop)

    def call(name, arguments=None):
        return {"id": name, "function": {"name": name, "arguments": arguments or {}}}

    assert agent._tool_call_can_run_concurrently(call("read_file", {"path": "a.py"}))
    assert agent._tool_call_can_run_concurrently(call("grep_search", {"query": "x"}))
    assert agent._tool_call_can_run_concurrently(call("bash", {"command": "git status"}))
    assert not agent._tool_call_can_run_concurrently(
        call("bash", {"command": "python -m pytest"})
    )
    assert not agent._tool_call_can_run_concurrently(call("todo", {"items": []}))
    assert not agent._tool_call_can_run_concurrently(call("update_scratchpad"))
    assert not agent._tool_call_can_run_concurrently(call("unknown_plugin_tool"))


def test_tool_execution_metadata_is_idempotent_and_defaults_to_serial():
    name = "_test_execution_metadata"

    def echo():
        return "ok"

    try:
        register(name, "test", {"type": "object", "properties": {}}, echo, execution="read")
        assert get_execution_mode(name) == "read"
        register(name, "test", {"type": "object", "properties": {}}, echo, execution="write")
        assert is_write_tool(name)
        register(name, "test", {"type": "object", "properties": {}}, echo)
        assert get_execution_mode(name) == "serial"

        try:
            register(name, "test", {}, echo, execution="unsafe")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid execution mode was accepted")
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_SPECS[:] = [spec for spec in TOOL_SPECS if spec["function"]["name"] != name]
