"""裁掉自动需求后，实际评审仍接收产物与历史负证据。"""
import hashlib

import pytest

from nz_coder.runtime.verification.artifact_evidence import generic_requirement_materials
from nz_coder.runtime.verification.domain.models import ArtifactFact
from test_completion_pruning_domain import domain, check, execute


def test_review_reads_complete_candidate_tests_and_docs_without_generic_requirement(tmp_path):
    owner = domain(tmp_path)
    for path, body in [("tests/test_cli.py", "# start\n" + "# middle\n" * 300 + "# important tail\n"),
                       ("README.md", "# Usage\nThis feature is not implemented yet.\n")]:
        target = tmp_path / path
        target.parent.mkdir(exist_ok=True)
        target.write_text(body)
        owner.observe_artifact(ArtifactFact(owner.scope.task, path, "create", 0, hashlib.sha256(target.read_bytes()).hexdigest()))
    material, digest = generic_requirement_materials(owner.snapshot(), tmp_path)
    assert "important tail" in material and "not implemented yet" in material
    assert digest
    with pytest.raises(ValueError, match="budget"):
        generic_requirement_materials(owner.snapshot(), tmp_path, maximum=100)


def test_deleted_candidate_document_is_reviewed_as_deletion_not_read_failure(tmp_path):
    owner = domain(tmp_path)
    owner.observe_artifact(ArtifactFact(owner.scope.task, "README.md", "delete", 1, "deleted", present=False))
    material, _ = generic_requirement_materials(owner.snapshot(), tmp_path)
    assert "README.md" in material and "delete" in material


def test_final_status_uses_same_domain_projection_for_missing_hard_and_plain_evidence(tmp_path):
    owner = domain(tmp_path)
    spec = check(owner, required=True)
    assert owner.snapshot().assess_completion("completed_unverified").status == "blocked"
    assert owner.snapshot().assess_completion("cancelled").status == "cancelled"
    execute(owner, spec, "failed", passed=False)
    assert owner.snapshot().assess_completion("completed").status == "blocked"
    plain = domain(tmp_path)
    from nz_coder.runtime.verification.domain.models import WorkspaceObservation, WorkspaceVersion
    plain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    assert plain.snapshot().assess_completion("completed").status == "completed_unverified"


def test_local_failure_then_current_full_pass_can_reach_real_review(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    command = "python -m unittest discover -s tests -v"
    old = 'python -m unittest discover -s tests -p "test_app.py" -v'
    files = [{"path": "app.py", "content": "VALUE = 1\n"},
        {"path": "tests/test_app.py", "content": "import unittest\nfrom app import VALUE\nclass AppTest(unittest.TestCase):\n def test_value(self): self.assertEqual(VALUE, 2)\n"}]
    actions = [[_tool("write_files_batch", files=files)], [_tool("bash", command=old)],
        [_tool("edit_file", path="app.py", old_text="VALUE = 1", new_text="VALUE = 2  # corrected")],
        [_tool("bash", command=command)], "Implementation and validation are complete."]
    result, state, trace, _requests, reviews, _permissions = _run(monkeypatch, tmp_path, actions,
        task=f"Create app.py with VALUE=2 and tests/test_app.py. Acceptance command: `{command}`.",
        allowed_commands=(old, command), limit=20)
    assert result.status.value == "completed", (result.error, result.final_text)
    assert reviews
    commands = [e.get("input", {}).get("command") for e in trace if e.get("event") == "tool_call" and e.get("executed")]
    assert commands.count(old) == 1
    assert any(e.get("event") == "sidecar_finished" and e.get("verdict") == "accept" for e in trace)


def test_ordinary_review_unavailable_finishes_unverified_without_empty_reanimation(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail('known failure')\n")
    command = "python -m unittest discover -s tests -v"
    actions = [[_tool("write_file", path="app.py", content="VALUE = 1\n")], [_tool("bash", command=command)], "Done."]
    result, _state, _trace, requests, _reviews, _permissions = _run(monkeypatch, tmp_path, actions,
        task="Create app.py with VALUE=1.", allowed_commands=(command,), semantic_response=RuntimeError("offline unavailable"), limit=20)
    assert result.status.value == "completed_unverified", (result.error, result.final_text)
    assert len(requests) == 3


def test_static_only_scaffold_cannot_skip_actual_delivery_review(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    command = "python -m py_compile app.py"
    actions = [[_tool("write_file", path="app.py", content="def total(items):\n    return 0\n")],
        [_tool("bash", command=command)], "Only the scaffold is present; calculating totals is not implemented."]
    result, _state, _trace, requests, reviews, _permissions = _run(monkeypatch, tmp_path, actions,
        task="Create app.py implementing a sum of the provided numbers.", allowed_commands=(command,),
        semantic_response=RuntimeError("offline review unavailable"), limit=20)
    assert reviews
    assert len(requests) == 3
    assert result.status.value == "completed_unverified"


def test_review_keeps_full_original_user_request_beyond_contract_summary():
    from nz_coder.runtime.verification.sidecar_verifier import build_verifier_context, build_verifier_user_message
    original = "Build the service. " + "Detailed context. " * 180 + "CRITICAL_ORIGINAL_REQUIREMENT_AT_END"
    context = build_verifier_context([{"role": "user", "content": "Continue."}], "Done.", file_edits=[],
        runtime_state={"initial_task_text": original})
    assert original in build_verifier_user_message(context)
