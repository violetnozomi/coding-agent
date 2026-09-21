"""Bootstrap must preserve the same clause boundaries as reference capture."""

from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts
from nz_coder.runtime.agent.task_policy import classify_instruction_paths


def test_bootstrap_does_not_extend_spec_authority_across_newline(tmp_path):
    task = "Follow SPEC.md\napi.py contains the current implementation."
    (tmp_path / "SPEC.md").write_text("Target contract")
    (tmp_path / "api.py").write_text("value = 1")
    roles = {r.path: r.role for r in classify_instruction_paths(task)}
    artifacts = {
        r.path: r
        for r in resolve_bootstrap_artifacts(task, workspace=tmp_path).artifacts
    }
    assert roles["api.py"] == "context"
    assert artifacts["api.py"].role == "context"
    assert artifacts["api.py"].authority == ""
    assert artifacts["SPEC.md"].authority == "task_spec"
