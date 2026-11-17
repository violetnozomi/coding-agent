"""真实unittest加载失败不能成为永远无法复测通过的业务义务。"""
from __future__ import annotations

from dataclasses import replace
import os
import re
import subprocess
import sys
import threading

import pytest

from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.rules import evaluate_check, classify_test_selection_failure
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _classify(fact):
    return replace(fact, test_selection_failure=classify_test_selection_failure(
        fact.command, outcome=fact.outcome, exit_code=fact.exit_code, test_count=fact.test_count,
        capabilities=fact.capabilities, output=fact.reason, output_complete=fact.output_complete,
        reliable=fact.reliable))


def _execute(root, *, required=False, mixed=False, single=False, fault=None):
    source = ("import unittest\nclass Cases(unittest.TestCase):\n"
              " def test_a(self): " + ("self.fail('actual behavior failure')" if mixed else "self.assertEqual(1, 1)") + "\n"
              " def test_b(self): self.assertEqual(2, 2)\n"
              " def test_actual(self): self.assertEqual(3, 3)\n")
    if fault == "setup":
        source += " def setUp(self): raise RuntimeError('actual setup error')\n"
    elif fault == "dependency":
        source = "import missing_dependency_for_loader_fixture\n" + source
    (root / "test_example.py").write_text(source)
    selectors = ([] if single else ["test_example.Cases.test_a", "test_example.Cases.test_b"]) + ["test_example.Cases.test_missing"]
    argv = [sys.executable, "-m", "unittest", *selectors, "-v"]
    result = subprocess.run(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=5)
    assert result.returncode == 1 and "unittest.loader._FailedTest" in result.stdout
    import shlex
    command = command_identity(shlex.join(argv), str(root))
    scope = RunScope(TaskScope("session", "task", str(root)), "run", "agent")
    version = WorkspaceVersion()
    spec = CheckSpec("selected-test", command, "test", required=required,
                     source="user_declared" if required else "model_execution")
    count = int(re.search(r"Ran (\d+) tests?", result.stdout)[1])
    fact = ExecutionFact("lookup-error", scope, command, "failed", version, version,
                         capabilities=("test",), exit_code=result.returncode,
                         test_count=count, reason=result.stdout)
    fact = _classify(fact)
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution(fact.execution_id, (spec.id,))
    domain.observe_execution(fact)
    return domain, spec, fact


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("required", [False, True])
def test_only_lookup_errors_remain_diagnostic_until_real_tests_pass(tmp_path, single, required):
    domain, spec, failed = _execute(tmp_path, single=single, required=required)
    snapshot = domain.snapshot()
    assert snapshot.decision(spec.id).status == "pending"
    assert snapshot.obligations == ()
    assert snapshot.targeted_pending and not snapshot.execution_ready
    assert snapshot.executions[0].exit_code == 1
    assert snapshot.executions[0].test_count == (1 if single else 3)
    command = spec.command.original.replace("test_missing", "test_actual")
    completed = subprocess.run(command_identity(command, str(tmp_path)).argv, cwd=tmp_path,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=5)
    assert completed.returncode == 0
    corrected = replace(spec, id="corrected-test", command=command_identity(command, str(tmp_path)))
    domain.register_check(corrected)
    domain.begin_execution("real-pass", (corrected.id,))
    domain.observe_execution(replace(failed, execution_id="real-pass", command=corrected.command,
                                    outcome="succeeded", exit_code=0, reason=completed.stdout, test_selection_failure=""))
    after = domain.snapshot()
    assert after.execution_ready is (not required)
    assert not after.targeted_pending
    assert after.decision(spec.id).status == "pending"  # 纠正没有把原错误命令变成通过。
    assert after.decision(corrected.id).status == "passed"
    assert after.executions[0].outcome == "failed"


def test_required_original_selector_stays_unconfirmed(tmp_path):
    domain, spec, _ = _execute(tmp_path, required=True)
    assert domain.snapshot().decision(spec.id).status == "pending"
    assert not domain.snapshot().ready_for_completion


@pytest.mark.parametrize("change", ["mixed", "setup", "dependency", "incomplete", "unreliable", "footer", "class", "traceback", "import", "script"])
def test_ambiguous_or_actual_failures_keep_negative_obligations(tmp_path, change):
    domain, spec, fact = _execute(tmp_path, mixed=change == "mixed", fault=change)
    if change == "incomplete":
        fact = replace(fact, output_complete=False)
    elif change == "unreliable":
        fact = replace(fact, reliable=False)
    elif change == "footer":
        fact = replace(fact, reason=fact.reason.replace("FAILED (errors=1)", "FAILED (errors=2)"))
    elif change == "class":
        fact = replace(fact, reason=fact.reason.replace("type object 'Cases'", "type object 'Other'"))
    elif change == "traceback":
        fact = replace(fact, reason=fact.reason.replace("AttributeError:", "Traceback (most recent call last):\nAttributeError:"))
    elif change == "import":
        fact = replace(fact, reason=fact.reason.replace("AttributeError: type object 'Cases' has no attribute 'test_missing'", "ImportError: actual dependency missing"))
    elif change == "script":
        command = command_identity("python custom_runner.py", str(tmp_path))
        spec, fact = replace(spec, command=command), replace(fact, command=command)
    fact = _classify(fact)
    assert evaluate_check(spec, fact, fact.after).status == "failed"


@pytest.mark.parametrize("mixed", [False, True])
def test_restore_reclassifies_only_proven_selection_obligations(tmp_path, mixed):
    domain, spec, fact = _execute(tmp_path, mixed=mixed)
    saved = domain.checkpoint()
    # 复现旧版本已写出的合法持久化输入，恢复器仍须完整校验引用及事实。
    saved["obligations"] = [{"check_id": spec.id, "failure_execution_ids": [fact.execution_id],
                              "resolved": False, "resolved_by": ""}]
    saved["failure_revisions"] = {spec.id: saved["revision"]}
    saved["decisions"][0].update(status="failed", execution_id=fact.execution_id, reason=fact.reason)
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id="continued"), version=fact.after)
    result = restored.snapshot()
    assert not result.rebuild_required
    assert result.executions[0].reason == fact.reason and result.executions[0].exit_code == 1
    assert bool(result.obligations) is mixed
    assert result.decision(spec.id).status == ("failed" if mixed else "pending")
    assert not result.execution_ready


def test_restore_keeps_real_failure_when_another_fact_is_only_lookup_error(tmp_path):
    _, _, lookup = _execute(tmp_path)
    domain, spec, failed = _execute(tmp_path, mixed=True)
    domain.begin_execution("later-lookup", (spec.id,))
    domain.observe_execution(replace(lookup, execution_id="later-lookup"))
    saved = domain.checkpoint()
    saved["obligations"][0]["failure_execution_ids"] = [failed.execution_id, "later-lookup"]
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id="continued"), version=failed.after)
    snapshot = restored.snapshot()
    assert not snapshot.rebuild_required
    assert snapshot.obligations and not snapshot.obligations[0].resolved
    assert set(snapshot.obligations[0].failure_execution_ids) == {"lookup-error", "later-lookup"}
    assert len(snapshot.executions) == 2
