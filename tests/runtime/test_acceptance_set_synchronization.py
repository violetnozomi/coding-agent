"""撤销当前验收不删除实际失败或在途执行，也不保留虚构的旧门槛。"""
from dataclasses import replace

from tests.runtime.test_verification_domain_state import _domain, _execute, _spec


def test_superseded_unrun_user_check_no_longer_blocks_completion(tmp_path):
    domain = _domain(tmp_path)
    old = replace(_spec(tmp_path), required=True, source="user_declared", requirement_ids=("R1",))
    new = replace(_spec(tmp_path, "new", "python new.py"), required=True, source="user_declared", requirement_ids=("R2",))
    domain.register_check(old)
    domain.replace_user_checks((new,))
    _execute(domain, new, "new-pass")
    snapshot = domain.snapshot()
    assert snapshot.ready_for_completion
    retired = next(spec for spec in snapshot.specs if spec.id == old.id)
    assert not retired.required and retired.requirement_ids == ()


def test_retiring_old_check_keeps_real_failure_and_its_matching_recheck(tmp_path):
    domain = _domain(tmp_path)
    old = replace(_spec(tmp_path), required=True, source="user_declared")
    domain.register_check(old)
    _execute(domain, old, "old-failure", passed=False)
    domain.replace_user_checks(())
    assert not domain.snapshot().ready_for_completion
    obligation, = domain.snapshot().obligations
    assert obligation.failure_execution_ids == ("old-failure",) and not obligation.resolved
    _execute(domain, old, "real-recheck")
    assert domain.snapshot().ready_for_completion


def test_retiring_old_check_keeps_live_execution(tmp_path):
    domain = _domain(tmp_path)
    old = replace(_spec(tmp_path), required=True, source="user_declared")
    domain.register_check(old)
    domain.begin_execution("still-running", (old.id,), verifies=True)
    domain.replace_user_checks(())
    assert not domain.snapshot().ready_for_completion
    assert not domain.execution_settled("still-running")
    domain.end_execution("still-running")
    assert domain.snapshot().ready_for_completion
