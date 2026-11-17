"""旧展示命令的失败应可复测结清，受限字节码清理不能丢失证据。"""
from __future__ import annotations

from dataclasses import replace
import shlex
import subprocess
import sys
import threading

import pytest

from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity, commands_equivalent, observed_check_subject
from nz_coder.runtime.verification.domain.models import CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from test_verification_interfaces import _source
from test_workspace_scope_execution import _scope_host, _execute


@pytest.mark.parametrize("prefix", ["dot", "absolute", "option", "quoted-space"])
def test_same_cwd_cd_keeps_raw_identity_but_matches_reliable_retry(tmp_path, prefix):
    root = tmp_path / "work space" if prefix == "quoted-space" else tmp_path
    root.mkdir(exist_ok=True)
    cwd = str(root.resolve())
    directory = "." if prefix == "dot" else shlex.quote(cwd)
    cd = "cd -- " if prefix == "option" else "cd "
    bare = "MODE=one pytest test_a.py --ignore test_b.py -q"
    raw = f"{cd}{directory} && {bare} 2>&1 | tail -4"
    identity = command_identity(raw, cwd)
    subject = observed_check_subject(identity)
    assert identity.original == raw
    assert subject is not None
    assert subject.environment == (("MODE", "one"),)
    assert commands_equivalent(identity, command_identity(bare, cwd), native_runner_kind="observed_output")
    assert not commands_equivalent(identity, command_identity(bare.replace("MODE=one", "MODE=two"), cwd), native_runner_kind="observed_output")
    assert not commands_equivalent(identity, command_identity("MODE=one pytest test_b.py --ignore test_a.py -q", cwd), native_runner_kind="observed_output")
    from nz_coder.intelligence.verification_planner import verification_success_is_reliable
    assert not verification_success_is_reliable(raw)


def test_direct_same_cwd_cd_can_match_bare_without_changing_actual_command(tmp_path):
    raw = f"cd {shlex.quote(str(tmp_path))} && python -m unittest discover -s tests -t . -v"
    identity = command_identity(raw, str(tmp_path))
    assert identity.original == raw and identity.opaque
    assert commands_equivalent(identity, command_identity("python -m unittest discover -s tests -t . -v", str(tmp_path)), native_runner_kind="observed_output")


@pytest.mark.parametrize("prefix", [
    "cd ..", "cd other", "cd -", "cd $PWD", "cd ~", "false && cd .",
    "cd .; true", "cd . && touch marker", "cd . && cd .", "cd\n.", "cd\r.",
])
def test_cd_with_unproved_scope_or_extra_operations_is_not_a_matching_wrapper(tmp_path, prefix):
    identity = command_identity(prefix + " && pytest test_a.py | tail -4", str(tmp_path))
    assert not commands_equivalent(identity, command_identity("pytest test_a.py", str(tmp_path)), native_runner_kind="observed_output")


@pytest.mark.parametrize("name", ["[ab]", "a?", "a*", "{a,b}", "$PWD", "~"])
def test_literal_cwd_with_shell_expansion_is_not_reinterpreted(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    raw = f"cd {root} && pytest test_a.py | tail -4"
    assert observed_check_subject(command_identity(raw, str(root))) is None


def test_same_cwd_wrapper_never_matches_a_different_execution_directory(tmp_path):
    raw = f"cd {shlex.quote(str(tmp_path))} && pytest test_a.py | tail -4"
    assert not commands_equivalent(command_identity(raw, str(tmp_path)),
        command_identity("pytest test_a.py", str(tmp_path / "other")), native_runner_kind="observed_output")


def test_current_checkpoint_reclassifies_unlabelled_wrapper_without_erasing_failure(tmp_path):
    cwd = str(tmp_path)
    raw = f"cd {shlex.quote(cwd)} && python -m unittest discover -s tests -t . -v 2>&1 | tail -40"
    scope = RunScope(TaskScope("session", "task", cwd), "before", "main")
    version = WorkspaceVersion()
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    spec = CheckSpec("original-failure", command_identity(raw, cwd), "test", source="failure_evidence")
    domain.register_check(spec)
    domain.begin_execution("failed", (spec.id,))
    domain.observe_execution(ExecutionFact("failed", scope, spec.command, "failed", version, version,
                                         capabilities=("test",), exit_code=1, reliable=False))
    saved = domain.checkpoint()
    assert saved["specs"][0]["native_runner_kind"] == ""
    restored = restore_domain(saved, lock=threading.RLock(), scope=replace(scope, interaction_run_id="after"), version=version)
    before = restored.snapshot()
    assert not before.rebuild_required
    assert before.specs[0].id == spec.id and before.specs[0].command == spec.command
    assert before.obligations == ()
    assert before.executions[0].execution_id == "failed" and before.executions[0].outcome == "failed"
    assert before.decision(spec.id).status == "failed"
    restored.begin_execution("recheck", (spec.id,))
    restored.observe_execution(ExecutionFact("recheck", restored.scope,
        command_identity("python -m unittest discover -s tests -t . -v", cwd), "succeeded", version, version,
        capabilities=("test",), exit_code=0, test_count=1))
    after = restored.snapshot()
    assert after.decision(spec.id).status == "passed"
    assert after.obligations == ()
    assert next(fact for fact in after.executions if fact.execution_id == "failed").outcome == "failed"


def _compiled_host(root, *, optimize=None):
    _source(root)
    if optimize is not None:
        subprocess.run([sys.executable, "-" + "O" * optimize, "-m", "py_compile", "pkg/module.py"],
                       cwd=root, check=True)
    host = _scope_host(root)
    result = _execute(host, "python -m py_compile pkg/module.py")
    assert not result.command_failed
    assert not host.verification_runtime.status()["verification_needed"]
    return host


@pytest.mark.parametrize("optimize", [None, 1, 2])
def test_deleting_unchanged_source_bytecode_keeps_pass_and_version(tmp_path, optimize):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path, optimize=optimize)
        version = host.runtime_state.mutation_generation
        assert list((tmp_path / "pkg/__pycache__").glob("*.pyc"))
        _execute(host, "rm -rf pkg/__pycache__ && echo cleaned")
        assert not (tmp_path / "pkg/__pycache__").exists()
        assert host.runtime_state.mutation_generation == version
        assert not host.verification_runtime.status()["verification_needed"]


@pytest.mark.parametrize("extra", ["payload.txt", "source.py", "fake.pyc", "module.cpython-313.notes.pyc", "orphan.cpython-313.pyc"])
def test_cache_directory_with_non_disposable_content_remains_a_mutation(tmp_path, extra):
    _source(tmp_path)
    cache = tmp_path / "pkg/__pycache__"
    cache.mkdir()
    (cache / extra).write_text("not disposable")
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        version = host.runtime_state.mutation_generation
        _execute(host, "rm -rf pkg/__pycache__")
        assert host.runtime_state.mutation_generation > version
        assert host.verification_runtime.status()["verification_needed"]


@pytest.mark.parametrize("operation", [
    "printf 'changed = True\\n' > pkg/module.py", "rm pkg/module.py", "printf data > other.json",
])
def test_cache_cleanup_with_other_mutation_never_preserves_pass(tmp_path, operation):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        version = host.runtime_state.mutation_generation
        _execute(host, "rm -rf pkg/__pycache__ && " + operation)
        assert host.runtime_state.mutation_generation > version
        assert host.verification_runtime.status()["verification_needed"]


def test_incomplete_cache_cleanup_proof_cannot_preserve_pass(tmp_path, monkeypatch):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        version = host.runtime_state.mutation_generation
        monkeypatch.setattr(host.workspace_snapshots, "verification_snapshot",
                            lambda **_: (_ for _ in ()).throw(SnapshotError("incomplete")))
        _execute(host, "rm -rf pkg/__pycache__")
        assert host.runtime_state.mutation_generation > version
        assert host.verification_runtime.status()["verification_needed"]


@pytest.mark.parametrize("external", ["source", "bytecode"])
def test_cache_cleanup_does_not_erase_unobserved_preceding_change(tmp_path, external):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        if external == "source":
            (tmp_path / "pkg/module.py").write_text("changed = True\n")
        else:
            next((tmp_path / "pkg/__pycache__").glob("*.pyc")).write_text("tampered")
        version = host.runtime_state.mutation_generation
        _execute(host, "rm -rf pkg/__pycache__")
        assert host.runtime_state.mutation_generation > version
        assert host.verification_runtime.status()["verification_needed"]


def test_cache_cleanup_after_unknown_interval_requires_fresh_verification(tmp_path, monkeypatch):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        capture = host.workspace_snapshots.verification_snapshot
        monkeypatch.setattr(host.workspace_snapshots, "verification_snapshot",
                            lambda **_: (_ for _ in ()).throw(SnapshotError("incomplete")))
        _execute(host, "true")
        unknown = host.runtime_state.mutation_generation
        monkeypatch.setattr(host.workspace_snapshots, "verification_snapshot", capture)
        _execute(host, "rm -rf pkg/__pycache__")
        assert host.runtime_state.mutation_generation > unknown
        assert host.verification_runtime.status()["verification_needed"]


def test_cache_cleanup_with_symlink_has_no_complete_proof(tmp_path):
    with scoped_workdir(tmp_path):
        host = _compiled_host(tmp_path)
        outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
        outside.write_text("preserved")
        (tmp_path / "pkg/__pycache__/link.cpython-313.pyc").symlink_to(outside)
        generation = host.runtime_state.mutation_generation
        _execute(host, "rm -rf pkg/__pycache__")
        assert outside.read_text() == "preserved"
        assert host.runtime_state.mutation_generation > generation
        assert host.verification_runtime.status()["verification_needed"]


@pytest.mark.parametrize("kind,consistent", [("django", True), ("", False)])
def test_restore_does_not_overwrite_matching_kind_or_trust_mismatched_raw_identity(tmp_path, kind, consistent):
    scope = RunScope(TaskScope("session", "task", str(tmp_path)), "before", "main")
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    raw = f"cd {shlex.quote(str(tmp_path))} && pytest test_a.py | tail -4"
    identity = command_identity(raw, str(tmp_path))
    if not consistent:
        identity = replace(identity, argv=("not-the-recorded-command",))
    domain.register_check(CheckSpec("preserved", identity, "test", native_runner_kind=kind))
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(), scope=replace(scope, interaction_run_id="after"), version=WorkspaceVersion())
    assert restored.snapshot().specs[0].native_runner_kind == kind


def test_same_cwd_python_check_caches_do_not_invalidate_the_check(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        result = _execute(host, f"cd {shlex.quote(str(tmp_path))} && python -m py_compile pkg/module.py")
        assert not result.command_failed
        assert host.runtime_state.mutation_generation == 0
        assert not host.verification_runtime.status()["verification_needed"]


def test_native_failed_cd_pipeline_then_reliable_check_and_cleanup_completes(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_feature.py").write_text("import unittest\nclass TestFeature(unittest.TestCase):\n def test_feature(self): self.assertEqual(2 + 2, 4)\n")
    # 聚焦发现一个真实测试，避免广泛测试策略在复测前拦截整个框架链路。
    suite = "python -m unittest discover -s tests -t . -p test_feature.py -v"
    failed = f"cd {shlex.quote(str(tmp_path))} && {suite} 2>&1 | tail -40"
    cleanup = "rm -rf tests/__pycache__"
    actions = [[_tool("bash", command=failed)],
               [_tool("write_file", path="tests/__init__.py", content="")],
               [_tool("verify_changed_files")],
               [_tool("bash", command=suite), _tool("bash", command=cleanup)],
               "Created the package marker. The focused unittest passed 1 test; bytecode cache removed."]
    result, state, trace, *_ = _run(monkeypatch, tmp_path, actions, limit=5,
        task=f"Create tests/__init__.py. Run the focused test. Acceptance command: `{suite}`. Clean the resulting bytecode and report actual outcomes.",
        allowed_commands=(failed, suite, cleanup), semantic_response="accept")
    assert result.status.value == "completed", (result.final_text, state)
    verification = result.metadata["verification"]
    assert not verification["verification_needed"]
    assert all(item["resolved_by"] for item in verification["test_obligations"])
    assert not (tmp_path / "tests/__pycache__").exists()
    assert any(event.get("event") == "verification_result" and event.get("command") == failed
               and event.get("status") == "failed" for event in trace)
