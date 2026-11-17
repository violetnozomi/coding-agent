"""正式manager准备、真实spawn及父方检索资源归属验收。"""

from dataclasses import replace
import threading
import time

import pytest

from tests.runtime.test_preparation_ownership import _manager, _finish


def start(
    manager, *, refs=None, prompt="complete", allowed_tools=None, read_only=False
):
    result = manager.start(
        [
            {
                "prompt": prompt,
                "isolation": "process",
                "target_paths": ["app.py"],
                "evidence_refs": refs,
                "allowed_tools": allowed_tools,
                "read_only": read_only,
            }
        ]
    )
    assert hasattr(result, "metadata"), result
    task = result.metadata["task_ids"][0]
    return task, manager._jobs[task]


def test_manager_saves_complete_worktree_before_diff_capture(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import prepared_evidence

    manager = _manager(tmp_path, monkeypatch)
    calls = []
    real = prepared_evidence.capture_diff_evidence

    def capture(refs, **kwargs):
        state = manager._load_raw(kwargs["child_session_id"])
        job = manager._jobs[state["session_id"]]
        assert state["worktree"] and not state.get("worktree_preparing")
        assert state["evidence_refs"] == refs == ["finding:check", "diff:app.py"]
        assert (
            kwargs["preparation"] is job.preparation_scope
            and not job.preparation_scope._sealed
        )
        assert kwargs["settlement_id"] == job.settlement_id
        calls.append(kwargs)
        return real(refs, **kwargs)

    monkeypatch.setattr(prepared_evidence, "capture_diff_evidence", capture)
    try:
        task, job = start(manager, refs=["finding:check", "diff:app.py"])
        assert job.done_event.wait(5)
        assert len(calls) == 1 and manager._load_raw(task)["status"] == "completed"
        assert job.supervisor._payload["prepared_workspace"].diff_evidence is not None
    finally:
        _finish(manager)


@pytest.mark.parametrize("failure", ["cancel", "timeout", "infra", "complete_save"])
def test_diff_infrastructure_failure_keeps_complete_worktree(
    tmp_path, monkeypatch, failure
):
    from nz_coder.runtime.agent import prepared_evidence, agent_manager
    from nz_coder.runtime.process.preparation import (
        PreparationInterrupted,
        PreparationFailure,
    )
    from nz_coder.protocol.public_error import PublicError

    manager = _manager(tmp_path, monkeypatch)
    calls, spawned = [], []

    def capture(*args, **kwargs):
        calls.append(kwargs)
        if failure == "infra":
            raise PreparationFailure(PublicError("preparation_io_error", "failed"))
        raise PreparationInterrupted("cancelled" if failure == "cancel" else "timeout")

    monkeypatch.setattr(prepared_evidence, "capture_diff_evidence", capture)

    def forbidden(*args, **kwargs):
        spawned.append(True)
        raise AssertionError("failed preparation reached spawn")

    monkeypatch.setattr(agent_manager, "SubagentProcessSupervisor", forbidden)
    if failure == "complete_save":
        real = manager._save

        def save(state):
            if (
                state.get("worktree")
                and not state.get("worktree_preparing")
                and not next(iter(manager._jobs.values())).frozen
            ):
                raise OSError("controlled complete-save failure")
            return real(state)

        monkeypatch.setattr(manager, "_save", save)
    try:
        task, job = start(manager, refs=["diff:app.py"])
        assert job.done_event.wait(5)
        assert not spawned
        assert len(calls) == (0 if failure == "complete_save" else 1)
        state = manager._load_raw(task)
        assert bool(state.get("worktree_preparing")) == (failure == "complete_save")
        assert state.get("worktree")
        assert state["status"] in {"cancelled", "timeout", "error"}
    finally:
        _finish(manager)


def test_resume_recaptures_effective_refs_for_new_invocation(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import prepared_evidence

    manager = _manager(tmp_path, monkeypatch)
    calls = []
    real = prepared_evidence.capture_diff_evidence

    def capture(refs, **kwargs):
        calls.append((refs, kwargs["settlement_id"]))
        return real(refs, **kwargs)

    monkeypatch.setattr(prepared_evidence, "capture_diff_evidence", capture)
    try:
        task, job = start(manager, refs=["diff:app.py"])
        assert job.done_event.wait(5)
        state = manager._load_raw(task)
        manager._launch(state, {"prompt": "complete", "evidence_refs": None})
        resumed = manager._jobs[task]
        assert resumed.done_event.wait(5)
        assert len(calls) == 2 and calls[0][0] == calls[1][0] == ["diff:app.py"]
        assert calls[0][1] != calls[1][1]
    finally:
        _finish(manager)


@pytest.mark.parametrize(
    "case", ["none", "identity_empty", "missing", "state_identity"]
)
def test_process_requires_bound_snapshot_even_with_empty_refs(
    tmp_path, monkeypatch, case
):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.prepared_evidence import capture_diff_evidence
    from tests.fixtures.process_lifecycle import make_preparation_scope
    from tests.fixtures.subagent_process_targets import invalid_evidence_probe
    from tests.runtime.test_parent_command_ownership import prepare_case, cleanup_case

    supervisor, scope, prepared, payload, errors = prepare_case(
        tmp_path, monkeypatch, invalid_evidence_probe
    )
    refs = ["diff:app.py"] if case == "missing" else []
    preparation = make_preparation_scope(
        tmp_path, child=payload["session_id"], settlement_id=payload["settlement_id"]
    )
    try:
        snapshot = capture_diff_evidence(
            refs,
            workspace=tmp_path,
            parent_session_id="parent",
            child_session_id=payload["session_id"],
            settlement_id=payload["settlement_id"],
            preparation=preparation,
        )
    finally:
        assert preparation.close(deadline=time.monotonic() + 2)
    if case == "none":
        snapshot = None
    if case == "identity_empty":
        snapshot = replace(snapshot, settlement_id="other")
    if case == "missing":
        snapshot = replace(snapshot, entries=())
    # 旧prepared对象也可携带该字段，使RED观察正式入口而非构造器异常。
    object.__setattr__(prepared, "diff_evidence", snapshot)
    supervisor._payload["evidence_refs"] = refs
    if case == "state_identity":
        state = subagent._load_subagent_state("parent", payload["session_id"], tmp_path)
        state["settlement_id"] = "changed-after-payload"
        subagent._save_subagent_state("parent", state, tmp_path)
    try:
        supervisor.start()
        outcome = supervisor.wait(
            threading.Event(), deadline=time.monotonic() + 4, stop_grace=0.05
        )
        assert not (tmp_path / "forbidden-provider").exists()
        assert not (tmp_path / "forbidden-child-git").exists()
        from nz_coder.runtime.agent.child_process_result import (
            read_child_process_result,
        )

        envelope = read_child_process_result(supervisor._result_path)
        assert envelope["public_error"]["code"] == "prepared_evidence_invalid"
        assert outcome.public_error is not None
        assert outcome.public_error.code in {
            "prepared_evidence_invalid",
            "child_command_channel_error",
        }

    finally:
        cleanup_case(supervisor, scope)


def retrieval_manager(workspace, monkeypatch, *, canonical=False):
    from nz_coder.runtime.agent import process_supervisor
    from tests.fixtures.subagent_process_targets import (
        retrieval_probe,
        retrieval_canonical_probe,
    )

    manager = _manager(workspace, monkeypatch)
    monkeypatch.setattr(
        process_supervisor,
        "_run_subagent_process",
        retrieval_canonical_probe if canonical else retrieval_probe,
    )
    return manager


def blocking_helpers(workspace, monkeypatch, *, preparation_diff=False):
    import os
    from tests.runtime.test_retrieval_tools import executable

    binary = workspace.parent / ("bin-" + workspace.name)
    binary.mkdir()
    ready = workspace / "helper-ready.json"
    source = f"""import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
parent={str(workspace)!r}
if Path(sys.argv[0]).name=='git':
    if os.getcwd()==parent and args[0]=='rev-parse': sys.exit(1)
    if os.getcwd()==parent and args[0]=='diff' and not {preparation_diff!r}:
        print('SNAPSHOT');sys.exit(0)
Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid(),'ppid':os.getppid(),'cwd':os.getcwd(),'sentinel':os.getenv('TEST_API_KEY')}}))
while True: time.sleep(.01)
"""
    compile(source, "<blocking-helper>", "exec")
    for name in ("git", "rg"):
        executable(binary / name, source)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))
    (workspace / "app.py").write_text("needle\n")
    return ready


def native_exited(record):
    assert record.process.returncode is not None and not record.thread.is_alive()
    assert all(s.closed and not s.thread.is_alive() for s in record.streams)


@pytest.mark.parametrize("tool", ["grep_search", "glob_search", "diff_status"])
@pytest.mark.parametrize("action", ["stop", "kill"])
def test_real_retrieval_stop_or_body_kill_retains_native_owner(
    tmp_path, monkeypatch, tool, action
):
    import os
    import json
    import signal
    from nz_coder.runtime.process import auxiliary
    from nz_coder.foundation.capability_lease import capability_leases
    from tests.fixtures.process_lifecycle import wait_until

    ready = blocking_helpers(tmp_path, monkeypatch)
    manager = retrieval_manager(tmp_path, monkeypatch)
    blocked = threading.Event()
    blocked.set()
    sweep = auxiliary.terminate_process_tree

    def final_sweep(process, **kwargs):
        result = sweep(process, **kwargs)
        return False if blocked.is_set() else result

    monkeypatch.setattr(auxiliary, "terminate_process_tree", final_sweep)
    popen = auxiliary.subprocess.Popen
    admitted = []

    def create(argv, **kwargs):
        if str(argv[0]).endswith("/rg") or (
            argv[0] == "git" and kwargs.get("cwd") != str(tmp_path)
        ):
            job = next(iter(manager._jobs.values()))
            request = next(iter(job.command_scope._requests.values()))
            assert request.auxiliary._record is not None
            admitted.append(request)
        return popen(argv, **kwargs)

    monkeypatch.setattr(auxiliary.subprocess, "Popen", create)
    try:
        task, job = start(
            manager, prompt="retrieval:" + tool, allowed_tools=[tool], read_only=True
        )
        wait_until(ready.exists, timeout=4)
        facts = json.loads(ready.read_text())
        assert facts["ppid"] == os.getpid() and len(admitted) == 1
        request = admitted[0]
        record = request.auxiliary._record
        lease_id = job.capability_lease_id
        if action == "stop":
            manager.stop([task], reason="test stop", timeout_ms=0)
        else:
            os.kill(job.supervisor.process.pid, signal.SIGKILL)
        wait_until(lambda: request.cancel_event.is_set(), timeout=3)
        assert not job.command_scope.close(deadline=time.monotonic())
        assert not job.done_event.is_set() and manager._active_jobs == 1
        assert capability_leases().get(lease_id) is not None
        assert request.auxiliary._record is record
        blocked.clear()
        assert job.done_event.wait(4)
        assert manager._active_jobs == 0 and capability_leases().get(lease_id) is None
        native_exited(record)
    finally:
        blocked.clear()
        _finish(manager)


@pytest.mark.parametrize("action", ["stop", "deadline"])
def test_real_diff_capture_cancel_or_deadline_never_spawns(
    tmp_path, monkeypatch, action
):
    import json
    import os
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import agent_manager
    from tests.fixtures.process_lifecycle import wait_until

    ready = blocking_helpers(tmp_path, monkeypatch, preparation_diff=True)
    manager = retrieval_manager(tmp_path, monkeypatch)
    if action == "deadline":
        monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 1)
    spawned = []

    def forbidden(*a, **kw):
        spawned.append(True)
        raise AssertionError("preparation reached Agent")

    monkeypatch.setattr(agent_manager, "SubagentProcessSupervisor", forbidden)
    try:
        task, job = start(
            manager, refs=["diff:app.py"], prompt="retrieval:evidence", read_only=True
        )
        wait_until(ready.exists, timeout=3)
        assert json.loads(ready.read_text())["ppid"] == os.getpid()
        record = job.preparation_scope._record
        assert manager._load_raw(task).get("worktree") and not manager._load_raw(
            task
        ).get("worktree_preparing")
        if action == "stop":
            manager.stop([task], timeout_ms=0)
        assert job.done_event.wait(4)
        assert not spawned and not (tmp_path / "model-probe.json").exists()
        native_exited(record)
        assert manager._load_raw(task)["status"] == (
            "cancelled" if action == "stop" else "timeout"
        )
    finally:
        _finish(manager)


def init_real_repo(workspace):
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True, check=True
        )

    git("init")
    (workspace / "app.py").write_text('needle = "BASE"\n')
    git("add", "app.py")
    git(
        "-c",
        "user.name=offline-test",
        "-c",
        "user.email=offline@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    (workspace / "app.py").write_text('needle = "OLD_SNAPSHOT"\n')
    return git


def test_snapshot_precedes_spawn_and_resume_samples_again(tmp_path, monkeypatch):
    import json
    from nz_coder.runtime.agent import agent_manager

    init_real_repo(tmp_path)
    manager = retrieval_manager(tmp_path, monkeypatch)
    original = agent_manager.SubagentProcessSupervisor.start
    invocations = []

    def spawn(self):
        evidence = self._payload["prepared_workspace"].diff_evidence
        invocations.append(evidence)
        if len(invocations) == 1:
            (tmp_path / "app.py").write_text('needle = "NEW_SNAPSHOT"\n')
        return original(self)

    monkeypatch.setattr(agent_manager.SubagentProcessSupervisor, "start", spawn)
    try:
        task, job = start(
            manager, refs=["diff:app.py"], prompt="retrieval:evidence", read_only=True
        )
        assert job.done_event.wait(6)
        assert manager._load_raw(task)["status"] == "completed"
        first = "\n".join(
            json.loads((tmp_path / "model-probe.json").read_text())["system"]
        )
        assert "OLD_SNAPSHOT" in first and "NEW_SNAPSHOT" not in first
        manager._launch(
            manager._load_raw(task),
            {"prompt": "retrieval:evidence", "evidence_refs": None},
        )
        resumed = manager._jobs[task]
        assert resumed.done_event.wait(6)
        assert manager._load_raw(task)["status"] == "completed"
        second = "\n".join(
            json.loads((tmp_path / "model-probe.json").read_text())["system"]
        )
        assert (
            "NEW_SNAPSHOT" in second
            and invocations[0].settlement_id != invocations[1].settlement_id
        )
        assert len(invocations) == 2
    finally:
        _finish(manager)


def test_all_declared_diff_sampling_shares_total_deadline(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import agent_manager
    from tests.fixtures.process_lifecycle import wait_until

    ready = blocking_helpers(tmp_path, monkeypatch, preparation_diff=True)
    for name in ("app.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x" * 6000)
    manager = retrieval_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 1)
    spawned = []

    def forbidden(*a, **kw):
        spawned.append(True)
        raise AssertionError("hidden diff exceeded deadline before Agent")

    monkeypatch.setattr(agent_manager, "SubagentProcessSupervisor", forbidden)
    try:
        task, job = start(
            manager,
            refs=[
                "file:app.py",
                "file:b.py",
                "file:c.py",
                "finding:extra resolution",
                "diff:late.py",
            ],
            prompt="retrieval:evidence",
            read_only=True,
        )
        wait_until(ready.exists, timeout=3)
        record = job.preparation_scope._record
        assert record.argv[-1] == "late.py"
        assert job.done_event.wait(4)
        assert manager._load_raw(task)["status"] == "timeout" and not spawned
        assert not (tmp_path / "model-probe.json").exists()
        native_exited(record)
    finally:
        _finish(manager)


@pytest.mark.parametrize("fault", ["sweep", "reader", "owner"])
def test_retrieval_recovery_requires_no_status_polling(tmp_path, monkeypatch, fault):
    from nz_coder.runtime.process import auxiliary
    from tests.fixtures.process_lifecycle import wait_until

    ready = blocking_helpers(tmp_path, monkeypatch)
    manager = retrieval_manager(tmp_path, monkeypatch)
    blocked = threading.Event()
    blocked.set()
    reached = threading.Event()
    if fault == "sweep":
        original = auxiliary.terminate_process_tree

        def sweep(*a, **kw):
            result = original(*a, **kw)
            if blocked.is_set():
                reached.set()
                return False
            return result

        monkeypatch.setattr(auxiliary, "terminate_process_tree", sweep)
    else:
        name = "_read" if fault == "reader" else "_execute"
        original = getattr(auxiliary.AuxiliaryCommandRunner, name)

        def run(self, *args):
            original(self, *args)
            reached.set()
            while blocked.is_set():
                time.sleep(0.005)

        monkeypatch.setattr(auxiliary.AuxiliaryCommandRunner, name, run)
    try:
        task, job = start(
            manager,
            prompt="retrieval:grep_search",
            allowed_tools=["grep_search"],
            read_only=True,
        )
        wait_until(ready.exists, timeout=4)
        request = next(iter(job.command_scope._requests.values()))
        record = request.auxiliary._record
        manager.stop([task], timeout_ms=0)
        assert reached.wait(3)
        assert not job.done_event.is_set() and manager._active_jobs == 1
        blocked.clear()
        assert job.done_event.wait(4)
        assert manager._active_jobs == 0
        native_exited(record)
    finally:
        blocked.clear()
        _finish(manager)


def test_retrieval_revocation_and_siblings_keep_original_identity(
    tmp_path, monkeypatch
):
    import json
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from tests.fixtures.process_lifecycle import wait_until

    monkeypatch.setenv("TEST_API_KEY", "offline-credential-sentinel")
    managers = []
    jobs = []
    facts = []
    try:
        for index, name in enumerate(("a", "b")):
            workspace = tmp_path / name
            workspace.mkdir()
            ready = blocking_helpers(workspace, monkeypatch)
            manager = retrieval_manager(workspace, monkeypatch)
            managers.append(manager)
            with scoped_run_settings(
                replace(
                    RunSettings.from_legacy_globals(),
                    bash_output_hard_limit_bytes=4096 * (index + 1),
                )
            ):
                task, job = start(
                    manager,
                    prompt="retrieval:grep_search",
                    allowed_tools=["grep_search"],
                    read_only=True,
                )
            jobs.append(job)
            wait_until(ready.exists, timeout=4)
            facts.append(json.loads(ready.read_text()))
            assert (
                job.command_scope.authority.run_settings.bash_output_hard_limit_bytes
                == 4096 * (index + 1)
            )
        assert all(f["sentinel"] is None for f in facts)
        assert facts[0]["cwd"] != facts[1]["cwd"]
        capability_leases().revoke_workspace(managers[0].workspace)
        assert jobs[0].done_event.wait(4)
        assert not jobs[1].done_event.is_set() and not jobs[1].cancel_event.is_set()
        assert not jobs[1].command_scope._requests[1].cancel_event.is_set()
    finally:
        for manager in managers:
            _finish(manager)


@pytest.mark.parametrize(
    "tool,no_rg",
    [
        ("grep_search", False),
        ("glob_search", False),
        ("diff_status", False),
        ("grep_search", True),
        ("glob_search", True),
    ],
)
def test_real_retrieval_success_preserves_results_and_policy(
    tmp_path, monkeypatch, tool, no_rg
):
    import json
    import shutil

    init_real_repo(tmp_path)
    (tmp_path / "note.txt").write_text("untracked")
    if no_rg:
        binary = tmp_path.parent / ("only-git-" + tmp_path.name)
        binary.mkdir()
        (binary / "git").symlink_to(shutil.which("git"))
        monkeypatch.setenv("PATH", str(binary))
    manager = retrieval_manager(tmp_path, monkeypatch)
    try:
        task, job = start(
            manager, prompt="retrieval:" + tool, allowed_tools=[tool], read_only=True
        )
        assert job.done_event.wait(6)
        state = manager._load_raw(task)
        assert state["status"] == "completed", state
        facts = json.loads((tmp_path / "model-probe.json").read_text())
        assert facts["model_calls"] == 2 and len(facts["tool_outputs"]) == 1
        from nz_coder.runtime.agent.subagent import _subagent_trace_dir
        import json as json_module

        events = [
            json_module.loads(line)
            for path in _subagent_trace_dir("parent", task, tmp_path).glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert [e["name"] for e in events if e.get("event") == "tool_call"] == [tool]

        assert (
            len(facts["tool_replies"]) == 1 and facts["tool_replies"][0]["tool"] == tool
        )
        reply = facts["tool_replies"][0]
        assert "app.py" in reply["text"] and not reply["text"].startswith("Error:")
        if tool == "grep_search":
            assert reply["metadata"]["matches"] == 1
        if tool == "glob_search":
            assert reply["metadata"]["count"] == 1
        if tool == "diff_status":
            assert (
                "note.txt" in reply["text"]
                and "has_non_empty_diff: true" in reply["text"]
            )
        assert facts["cwd"] == str(job.command_scope.authority.execution_root)
        assert (
            job.command_scope._policy.fingerprint
            == job.command_scope.authority.execution_policy.fingerprint
        )
        assert all(
            request.worker is None or not request.worker.is_alive()
            for request in job.command_scope._requests.values()
        )
    finally:
        _finish(manager)


def test_a2_retry_after_retrieval_never_replays_or_double_charges(
    tmp_path, monkeypatch
):
    from nz_coder.runtime.agent import prepared_evidence

    init_real_repo(tmp_path)
    manager = retrieval_manager(tmp_path, monkeypatch, canonical=True)
    blocked = threading.Event()
    blocked.set()
    reached = threading.Event()
    snapshots = []
    submitted = []
    capture = prepared_evidence.capture_diff_evidence

    def sample(*a, **kw):
        snapshots.append(kw["settlement_id"])
        return capture(*a, **kw)

    monkeypatch.setattr(prepared_evidence, "capture_diff_evidence", sample)
    submit = manager._settlements.submit

    def send(value):
        submitted.append(value)
        return submit(value)

    monkeypatch.setattr(manager._settlements, "submit", send)
    save = manager._settlements._save_state

    def save_state(state):
        if blocked.is_set():
            reached.set()
            raise OSError("controlled settlement storage failure")
        return save(state)

    monkeypatch.setattr(manager._settlements, "_save_state", save_state)
    try:
        task, job = start(
            manager,
            refs=["diff:app.py"],
            prompt="retrieval:diff_status",
            allowed_tools=["diff_status"],
            read_only=True,
        )
        assert reached.wait(6) and job.frozen
        assert len(submitted) == 1 and snapshots == [job.settlement_id]
        candidate = submitted[0]
        assert candidate.state["child_result"]["usage"]["total"] == 18
        assert candidate.state["child_result"]["cost"] == 0.125
        assert manager._active_jobs == 1 and not job.done_event.is_set()
        scope = job.command_scope
        request_count = scope._high_water
        manager.stop([task], timeout_ms=0)
        assert (
            candidate.state["status"] == "completed"
            and candidate.state["child_result"]["cost"] == 0.125
        )
        blocked.clear()
        manager._settlements.wake(recheck=True)
        assert job.done_event.wait(4)
        assert (
            len(submitted) == len(snapshots) == 1
            and scope._high_water == request_count == 1
        )
        assert (
            manager._load_raw(task)["child_result"]["cost"] == 0.125
            and manager._active_jobs == 0
        )
        assert manager._workflow.snapshot()["counts"]["completed"] == 1
    finally:
        blocked.clear()
        manager._settlements.wake(recheck=True)
        _finish(manager)
