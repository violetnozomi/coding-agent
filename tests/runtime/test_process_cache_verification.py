"""真实后台 Python 检查的缓存不能使通过失效，也不能掩盖源码或损坏缓存写入。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shlex
import socket
import subprocess
import sys
import threading
import time

import pytest

from nz_coder.runtime.adapters.verification_facts import command_receipt
from nz_coder.runtime.process.process_service import ProcessService
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_scope_execution import execute_observed, python_verification_artifact_paths
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.tool_platform.execution import ToolExecutionResult
from tests.runtime.test_batch_verification_boundaries import _local_launch
from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.verification.domain.models import ArtifactFact
from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("process cache regression attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _project(root, *, effect="", fail=False):
    component = root / "component"
    (component / "tests").mkdir(parents=True)
    (component / "app.py").write_text("VALUE = 2\n")
    (component / "tests/__init__.py").write_text("")
    (component / "tests/test_app.py").write_text(
        "import importlib.util\nfrom pathlib import Path\nimport unittest\nimport app\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_count(self):\n"
        f"        self.assertEqual(app.VALUE, {999 if fail else 2})\n"
        "    def test_storage(self):\n"
        "        print('REAL_PROCESS_CACHE_TEST', flush=True)\n"
        "        self.assertEqual(app.VALUE, 2)\n"
        + "".join(f"        {line}\n" for line in effect.splitlines())
    )
    return component


def _command(*, no_bytecode=False):
    flags = " -B" if no_bytecode else ""
    return f"{shlex.quote(sys.executable)}{flags} -m unittest tests.test_app.AppTest -v"


@contextmanager
def _verification(root, component, command, monkeypatch, *, producer="process"):
    # 只观察真实终态提交以建立确定性屏障；执行、快照、缓存核对和域裁决均保留生产实现。
    plan = {"stages": [{"name": "targeted", "required": True, "commands": [
        {"command": command, "cwd": str(component), "required": True},
    ]}]}
    scenario = VerificationScenario(workspace=root, plan=plan, require_targeted=True)
    service = ProcessService(root)
    done, errors = threading.Event(), []
    receive = scenario.runtime._receive_local_terminal

    def receive_terminal(terminal):
        try:
            return receive(terminal)
        except BaseException as error:
            errors.append(error)
            raise
        finally:
            done.set()

    monkeypatch.setattr(scenario.runtime, "_receive_local_terminal", receive_terminal)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(root):
            arguments = {"command": command, "workdir": "component"}
            if producer == "process":
                arguments.update(operation="start", tty=False)
            call = {"id": "cache-verification", "function": {"name": producer, "arguments": arguments}}
            start = scenario.runtime.begin_tool(call)

            def execute():
                if producer == "process":
                    return _local_launch(scenario, service, start, arguments)
                actual = subprocess.run(shlex.split(command), cwd=component,
                    capture_output=True, text=True, timeout=5)
                output = actual.stdout + actual.stderr
                receipt = command_receipt("bash", command, str(component),
                    outcome="failed" if actual.returncode else "succeeded",
                    exit_code=actual.returncode, output=output)
                return ToolExecutionResult("bash", arguments, output, True, False,
                    actual.returncode != 0, False, execution_facts=(receipt,), execution_id=start.execution_id)

            result = execute_observed(scenario.store, start.version.generation, execute,
                on_before=lambda reference: scenario.runtime.record_before(start, reference))
            scenario.runtime.record_result(result)
            if producer == "process":
                assert json.loads(result.output)["operation"] == "start", result.output
                assert done.wait(5), "real process terminal was not delivered"
                assert service.wait_terminal_delivery(deadline=time.monotonic() + 5)
                assert not errors
            assert scenario.domain.execution_settled(start.execution_id)
            fact, = scenario.domain.snapshot().executions
            assert fact.command.cwd == str(component)
            assert fact.test_count == 2 and fact.output_complete and fact.reliable
            assert "REAL_PROCESS_CACHE_TEST" in fact.reason
            yield scenario, fact
    finally:
        service.close(deadline=time.monotonic() + 5)
        scenario.runtime.close()


@pytest.mark.parametrize("cache", ["cold", "warm", "no-bytecode"])
def test_process_test_success_remains_current_with_normal_cache(tmp_path, monkeypatch, cache):
    component = _project(tmp_path)
    command = _command(no_bytecode=cache == "no-bytecode")
    if cache == "warm":
        warmed = subprocess.run(shlex.split(command), cwd=component,
            capture_output=True, text=True, timeout=5)
        assert warmed.returncode == 0, warmed.stderr
        assert tuple(component.rglob("*.pyc"))
    with _verification(tmp_path, component, command, monkeypatch) as (scenario, fact):
        assert fact.exit_code == 0 and fact.outcome == "succeeded"
        if cache == "cold":
            assert tuple(component.rglob("*.pyc"))
        if cache == "no-bytecode":
            assert not tuple(component.rglob("*.pyc"))
        assert fact.before.acceptance_generation == fact.after.acceptance_generation
        assert scenario.state.mutation_generation == 0
        status = scenario.runtime.status()
        assert status["tests_passed"] is True
        assert status["completion_ready"] is True


def test_process_success_with_source_side_effect_cannot_prove_final_version(tmp_path, monkeypatch):
    component = _project(tmp_path, effect="Path('app.py').write_text('VALUE = 2\\nSIDE_EFFECT = True\\n')")
    with _verification(tmp_path, component, _command(), monkeypatch) as (scenario, fact):
        assert fact.exit_code == 0 and fact.outcome == "succeeded"
        assert "SIDE_EFFECT = True" in (component / "app.py").read_text()
        assert fact.after.acceptance_generation > fact.before.acceptance_generation
        assert "component/app.py" in scenario.state.changed_files
        assert scenario.runtime.status()["tests_passed"] is not True
        assert not scenario.runtime.status()["completion_ready"]


def test_process_nonzero_test_keeps_failure_obligation_despite_normal_cache(tmp_path, monkeypatch):
    component = _project(tmp_path, fail=True)
    with _verification(tmp_path, component, _command(), monkeypatch) as (scenario, fact):
        assert fact.exit_code == 1 and fact.outcome == "failed"
        status = scenario.runtime.status()
        assert status["tests_passed"] is False
        assert not status["completion_ready"]
        assert any(fact.execution_id in obligation.failure_execution_ids and not obligation.resolved
                   for obligation in scenario.domain.snapshot().obligations)


@pytest.mark.parametrize("producer", ["bash", "process"])
@pytest.mark.parametrize("corruption", ["bad-magic", "truncated"])
def test_single_unittest_corrupting_pyc_is_a_real_mutation(tmp_path, monkeypatch, producer, corruption):
    damage = "b'BAD!' + body[4:]" if corruption == "bad-magic" else "body[:8]"
    effect = (
        "cache = Path(importlib.util.cache_from_source(app.__file__))\n"
        "body = cache.read_bytes()\n"
        f"cache.write_bytes({damage})"
    )
    component = _project(tmp_path, effect=effect)
    # 破坏发生在单条 unittest 内部；不能靠复合命令分类先拒绝整个命令来让反例通过。
    with _verification(tmp_path, component, _command(), monkeypatch, producer=producer) as (scenario, fact):
        assert fact.exit_code == 0 and fact.outcome == "succeeded"
        cache = Path(importlib.util.cache_from_source(str(component / "app.py")))
        if corruption == "bad-magic":
            assert cache.read_bytes()[:4] == b"BAD!"
        else:
            assert len(cache.read_bytes()) == 8
        assert fact.after.acceptance_generation > fact.before.acceptance_generation
        assert str(cache.relative_to(tmp_path)) in scenario.state.changed_files
        assert scenario.runtime.status()["tests_passed"] is not True
        assert not scenario.runtime.status()["completion_ready"]


def _cache_transition(root, mode):
    source = root / "app.py"
    source.write_text("VALUE = 2\n")
    store = WorkspaceSnapshotStore(root, root / ".nz-coder" / "cache-snapshots")
    before = store.verification_snapshot()
    py_compile.compile(str(source), doraise=True, invalidation_mode=mode)
    cache = Path(importlib.util.cache_from_source(str(source)))
    return source, cache, store, before


def _cache_is_ignored(store, before, after):
    result = ToolExecutionResult("bash", {"command": "python -m unittest tests.test_app -v"},
        "Ran 2 tests\nOK\n", True, False, False, False, workspace_scope={
            "workspace": str(store.workspace), "generation": 0, "before": before, "after": after,
        })
    changes = store.verification_changes(before, after)
    return bool(changes) and python_verification_artifact_paths(result, changes, store) == set(changes)


@pytest.mark.parametrize("mode,damage,ignored", [
    (py_compile.PycInvalidationMode.TIMESTAMP, "valid", True),
    (py_compile.PycInvalidationMode.CHECKED_HASH, "valid", True),
    (py_compile.PycInvalidationMode.UNCHECKED_HASH, "valid", True),
    (py_compile.PycInvalidationMode.TIMESTAMP, "magic", False),
    (py_compile.PycInvalidationMode.TIMESTAMP, "flags", False),
    (py_compile.PycInvalidationMode.TIMESTAMP, "mtime", False),
    (py_compile.PycInvalidationMode.TIMESTAMP, "size", False),
    (py_compile.PycInvalidationMode.CHECKED_HASH, "source-hash", False),
    (py_compile.PycInvalidationMode.UNCHECKED_HASH, "source-hash", False),
])
def test_cache_exemption_checks_pyc_header_source_identity(tmp_path, mode, damage, ignored):
    _source, cache, store, before = _cache_transition(tmp_path, mode)
    body = bytearray(cache.read_bytes())
    if damage == "magic":
        body[:4] = b"BAD!"
    elif damage == "flags":
        body[4:8] = (4).to_bytes(4, "little")
    elif damage in {"mtime", "size"}:
        offset = 8 if damage == "mtime" else 12
        value = (int.from_bytes(body[offset:offset + 4], "little") + 1) % (2**32)
        body[offset:offset + 4] = value.to_bytes(4, "little")
    elif damage == "source-hash":
        body[8:16] = b"\0" * 8
    cache.write_bytes(body)
    after = store.verification_snapshot()
    assert _cache_is_ignored(store, before, after) is ignored


@pytest.mark.parametrize("changed_after_snapshot", ["source", "cache"])
def test_cache_exemption_binds_reads_to_after_snapshot(tmp_path, changed_after_snapshot):
    source, cache, store, before = _cache_transition(tmp_path, py_compile.PycInvalidationMode.TIMESTAMP)
    after = store.verification_snapshot()
    if changed_after_snapshot == "source":
        original = source.stat()
        # 相同长度和时间戳仍不能代替源码内容身份；旧 timestamp pyc 的头部仍匹配。
        source.write_text("VALUE = 3\n")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    else:
        # 新缓存头部也合法，仍须证明读到的字节就是 after 快照中的字节。
        py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    assert _cache_is_ignored(store, before, after) is False


def _wait_for_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"child did not create {path.name}"
        time.sleep(.005)


@contextmanager
def _cache_created_inside_read(tmp_path, monkeypatch, *, arbitrary=False,
                               spoof_command=False, spoof_metadata=False,
                               unknown_pid=False, other_runtime=False,
                               non_builtin=False, side_effect="", operation="read", denied=False):
    workspace = tmp_path / "workspace"
    controls = tmp_path / "controls"
    controls.mkdir()
    ready, release, imported, finish = (controls / name for name in ("ready", "release", "imported", "finish"))
    effect = (
        "import time\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.005)\n"
        "import late\n"
        + (side_effect + "\n" if side_effect else "")
        + f"Path({str(imported)!r}).write_text('imported')\n"
        + "print('LATE_CACHE_IMPORTED', flush=True)\n"
        + f"while not Path({str(finish)!r}).exists(): time.sleep(.005)"
    )
    component = _project(workspace, effect=effect)
    (component / "late.py").write_text("LATE_VALUE = 17\n")
    # 初始模块预热，唯一新缓存由真实检查在读取边界内导入 late 产生。
    for source in component.rglob("*.py"):
        if source.name != "late.py":
            py_compile.compile(str(source), doraise=True)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    command = _command() if not arbitrary else (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote("import unittest; unittest.main(module='tests.test_app', verbosity=2)")
    )
    contract = TaskContract(requirements=(Requirement(
        "R2", "Add explicit tests", "test", ("component/tests/test_app.py",), "semantic"),))
    plan = {"stages": [{"name": "targeted", "required": True, "commands": [
        {"command": command, "cwd": str(component), "required": True},
    ]}]}
    launcher = VerificationScenario(contract=contract, workspace=workspace, plan=plan, require_targeted=True)
    test_file = component / "tests/test_app.py"
    launcher.domain.observe_artifact(ArtifactFact(launcher.domain.scope.task,
        "component/tests/test_app.py", "create", 0, hashlib.sha256(test_file.read_bytes()).hexdigest()))
    reader = VerificationScenario(workspace=workspace, plan={"stages": []}) if other_runtime else launcher
    service = ProcessService(workspace)
    done, errors = threading.Event(), []
    receive = launcher.runtime._receive_local_terminal

    def receive_terminal(terminal):
        try:
            return receive(terminal)
        except BaseException as error:
            errors.append(error)
            raise
        finally:
            done.set()

    monkeypatch.setattr(launcher.runtime, "_receive_local_terminal", receive_terminal)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(workspace):
            arguments = {"operation": "start", "command": command, "workdir": "component", "tty": False}
            origin = launcher.runtime.begin_tool({"id": "read-owner-start", "function": {
                "name": "process", "arguments": arguments}})
            started = execute_observed(launcher.store, origin.version.generation,
                lambda: _local_launch(launcher, service, origin, arguments),
                on_before=lambda ref: launcher.runtime.record_before(origin, ref))
            launcher.runtime.record_result(started)
            process_id = json.loads(started.output)["process"]["process_id"]
            _wait_for_file(ready)
            selected_id = "proc_unknown" if unknown_pid else process_id
            read_args = {"operation": operation, "process_id": selected_id,
                         "cursor": 0, "max_bytes": 65536, "wait_seconds": 1}
            if spoof_command:
                read_args["command"] = _command()
            if non_builtin:
                from nz_coder.tools import TOOL_HANDLERS
                from nz_coder.tools.process import run_process
                monkeypatch.setitem(TOOL_HANDLERS, "process", lambda **kwargs: run_process(**kwargs))
            read_start = reader.runtime.begin_tool({"id": "cache-observing-read", "function": {
                "name": "process", "arguments": read_args}})

            def read_actual_process():
                from nz_coder.tools.process import execute_process_local
                # 门闩只保证真实缓存创建发生在真实 before/after 快照之间。
                release.write_text("release")
                _wait_for_file(imported)
                raw = "Denied: user rejected the read" if denied else execute_process_local(
                    read_args, service=service, owner_session_id="boundary-session",
                    owner_agent_id="main", event_bus=None)
                metadata = dict(getattr(raw, "metadata", {}))
                if spoof_metadata:
                    metadata.update(command=_command(), execution_id=origin.execution_id,
                                    process_id=process_id, owner_agent_id="test")
                return ToolExecutionResult("process", read_args, str(raw), not denied,
                    denied or str(raw).startswith("Error:"), False, False, metadata=metadata,
                    permission_denied=denied,
                    execution_facts=getattr(raw, "execution_facts", ()), execution_id=read_start.execution_id)

            read_result = execute_observed(reader.store, read_start.version.generation, read_actual_process,
                on_before=lambda ref: reader.runtime.record_before(read_start, ref))
            reader.runtime.record_result(read_result)
            cache = Path(importlib.util.cache_from_source(str(component / "late.py")))
            assert cache.is_file(), "the real verification process must create a real pyc"
            finish.write_text("finish")
            assert done.wait(5), "the original launch must deliver its real terminal"
            assert service.wait_terminal_delivery(deadline=time.monotonic() + 5)
            assert not errors
            facts = [fact for fact in launcher.domain.snapshot().executions if fact.execution_id == origin.execution_id]
            assert len(facts) == 1
            fact = facts[0]
            assert fact.exit_code == 0 and fact.outcome == "succeeded"
            assert fact.test_count == 2 and fact.output_complete and fact.reliable
            yield launcher, reader, fact
    finally:
        release.write_text("release")
        finish.write_text("finish")
        service.close(deadline=time.monotonic() + 5)
        if reader is not launcher:
            reader.runtime.close()
        launcher.runtime.close()


@pytest.mark.parametrize("operation", ["read", " READ "], ids=["read", "normalized-read"])
def test_read_of_owned_python_check_preserves_original_fact_and_test_evidence(tmp_path, monkeypatch, operation):
    with _cache_created_inside_read(tmp_path, monkeypatch, operation=operation) as (scenario, _reader, fact):
        assert fact.before.acceptance_generation == fact.after.acceptance_generation == 0
        assert scenario.state.mutation_generation == 0
        assert scenario.runtime.status()["tests_passed"] is True
        r2 = next(item for item in scenario.domain.snapshot().requirements if item.requirement.id == "R2")
        assert r2.status == "candidate"  # 测试真实通过，但不按文件名认证语义需求。
        assert fact in scenario.domain.snapshot().executions


@pytest.mark.parametrize("options", [
    {"arbitrary": True},
    {"arbitrary": True, "spoof_command": True},
    {"arbitrary": True, "spoof_metadata": True},
    {"unknown_pid": True, "spoof_command": True, "spoof_metadata": True},
    {"other_runtime": True, "spoof_command": True, "spoof_metadata": True},
    {"non_builtin": True},
    {"side_effect": "Path('ordinary.json').write_text('{\"feature\": true}')"},
    {"side_effect": "Path('app.py').write_text('VALUE = 23\\n')"},
    {"operation": "status", "spoof_command": True},
    {"denied": True, "spoof_command": True, "spoof_metadata": True},
], ids=["arbitrary-python", "forged-read-command", "forged-public-metadata", "unknown-pid",
        "other-runtime", "non-builtin-read", "ordinary-write", "source-write", "forged-status-command", "denied-read"])
def test_process_read_cache_owner_never_hides_untrusted_or_noncache_changes(tmp_path, monkeypatch, options):
    with _cache_created_inside_read(tmp_path, monkeypatch, **options) as (_launcher, reader, _fact):
        assert reader.state.source_mutation_generation > 0
        assert reader.state.acceptance_mutation_generation > 0


@pytest.mark.parametrize("operation", ["start", " START "])
def test_fast_process_keeps_owned_receipt_when_terminal_precedes_start_result(tmp_path, monkeypatch, operation):
    component = _project(tmp_path)
    command = _command()
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    service = ProcessService(tmp_path)
    done = threading.Event()
    receive = scenario.runtime._receive_local_terminal

    def receive_terminal(terminal):
        try:
            return receive(terminal)
        finally:
            done.set()

    monkeypatch.setattr(scenario.runtime, "_receive_local_terminal", receive_terminal)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(tmp_path):
            arguments = {"operation": operation, "command": command, "workdir": "component", "tty": False}
            start = scenario.runtime.begin_tool({"id": "fast-process", "function": {
                "name": "process", "arguments": arguments}})
            result = execute_observed(scenario.store, start.version.generation,
                lambda: _local_launch(scenario, service, start, arguments),
                on_before=lambda ref: scenario.runtime.record_before(start, ref))
            # 不能在持有快照锁时等待终态回调；这里生产执行边界已经正常释放。
            handle = json.loads(result.output)["process"]
            deadline = time.monotonic() + 3
            while service.get(handle["process_id"], owner_session_id="boundary-session").status == "running":
                assert time.monotonic() < deadline
                time.sleep(.005)
            assert done.wait(3)
            assert scenario.domain.execution_settled(start.execution_id)
            scenario.runtime.record_result(result)
            receipt = getattr(start, "process_start_receipt", None)
            assert receipt is not None, "the real launch ownership must survive an earlier terminal"
            assert receipt.execution_id == handle["process_id"]
            assert receipt.command == command and receipt.cwd == str(component)
    finally:
        service.close(deadline=time.monotonic() + 5)
        scenario.runtime.close()
