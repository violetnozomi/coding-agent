"""自然交付的身份、事实与最终冻结必须指向同一候选。"""
from __future__ import annotations

from uuid import uuid4

import pytest

from test_completion_pruning_domain import domain, check, execute
from nz_coder.runtime.verification.domain.models import ReviewFact


def reviewed(owner, *, accepted=True, candidate=None):
    snapshot = owner.snapshot()
    owner.observe_review(ReviewFact(uuid4().hex, owner.scope.task, (),
        snapshot.version, "materials", accepted, basis_fingerprint=owner.review_basis(),
        candidate_fingerprint=snapshot.candidate_fingerprint if candidate is None else candidate))


def test_new_candidate_cannot_reuse_previous_acceptance(tmp_path):
    owner = domain(tmp_path)
    owner.set_completion_candidate("candidate-a")
    reviewed(owner)
    assert owner.snapshot().assess_completion("completed").status == "completed"
    revision = owner.snapshot().revision
    owner.set_completion_candidate("candidate-b")
    current = owner.snapshot()
    assert current.revision > revision
    assert current.assess_completion("completed").status == "completed_unverified"
    assert current.assess_completion("completed").review_required
    assert not current.ready_for_completion


def test_latest_negative_review_overrides_earlier_acceptance(tmp_path):
    owner = domain(tmp_path)
    owner.set_completion_candidate("candidate")
    reviewed(owner)
    reviewed(owner, accepted=False)
    assert owner.snapshot().assess_completion("completed").status == "blocked"
    assert not owner.snapshot().ready_for_completion


def test_review_cannot_satisfy_exact_failed_command(tmp_path):
    owner = domain(tmp_path)
    required = check(owner, required=True)
    execute(owner, required, "failed", passed=False)
    owner.set_completion_candidate("candidate")
    reviewed(owner)
    assessment = owner.snapshot().assess_completion("completed")
    assert assessment.status == "blocked"
    assert required.id in assessment.blocking_ids
    assert owner.snapshot().executions[0].outcome == "failed"


def test_candidate_change_invalidates_freeze_revision(tmp_path):
    owner = domain(tmp_path)
    owner.set_completion_candidate("candidate-a")
    reviewed(owner)
    revision = owner.snapshot().revision
    owner.set_completion_candidate("candidate-b")
    with pytest.raises(ValueError, match="revision changed"):
        owner.seal(expected_revision=revision, terminal_reason="completed")
    with pytest.raises(ValueError, match="unconfirmed"):
        owner.seal(expected_revision=owner.snapshot().revision, terminal_reason="completed")


def test_identical_candidate_does_not_change_revision(tmp_path):
    owner = domain(tmp_path)
    owner.set_completion_candidate("candidate")
    revision = owner.snapshot().revision
    owner.set_completion_candidate("candidate")
    assert owner.snapshot().revision == revision


def test_candidate_digest_includes_structured_output():
    from nz_coder.runtime.verification.domain.models import candidate_fingerprint

    first = candidate_fingerprint("result", {"tests_passed": 1})
    assert first != candidate_fingerprint("result", {"tests_passed": 999})
    assert candidate_fingerprint("result", {"a": 1, "b": 2}) == candidate_fingerprint("result", {"b": 2, "a": 1})


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "error", "max_turns", "blocked"])
def test_noncompletion_status_is_preserved(tmp_path, status):
    owner = domain(tmp_path)
    owner.set_completion_candidate("candidate")
    assert owner.snapshot().assess_completion(status).status == status
