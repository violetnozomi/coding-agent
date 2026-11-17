"""Bootstrap must preserve the same clause boundaries as reference capture."""

import pytest

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


@pytest.mark.parametrize("capture_references", [False, True])
def test_project_followup_keeps_requirements_as_input(tmp_path, capture_references):
    """真实项目提示只能把明确授权的成品加入修改合同。"""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    task = (
        "Finish the remaining project now: the previous app.js write was denied "
        "by the test operator while stopping, not a user requirement. "
        "You are explicitly authorized to write taskboard/web/app.js and README.md "
        "in this workspace. Keep the existing backend unchanged. "
        "Implement every UI behavior required by requirements.md using the current HTML/CSS. "
        "Write one file per call, then run tests and HTTP smoke, stop any servers, "
        "and report real results and remaining gaps. Do not ask questions or change model settings."
    )
    (tmp_path / "requirements.md").write_text("The task board must support task creation.\n")
    state = RuntimeState()

    state.apply_current_round_instruction(
        task, workspace=tmp_path, capture_task_references=capture_references,
    )

    assert state.requested_paths == ["taskboard/web/app.js", "README.md"]
    assert [item["expected_artifacts"] for item in state.task_contract["requirements"]] == [
        ["taskboard/web/app.js"], ["README.md"],
    ]
    assert (tmp_path / "requirements.md").read_text() == "The task board must support task creation.\n"
    if capture_references:
        artifacts = resolve_bootstrap_artifacts(task, workspace=tmp_path).artifacts
        reference = next(item for item in artifacts if item.path == "requirements.md")
        assert reference.role == "task_reference"
        assert reference.authority == "task_spec"
        assert reference.required is False


@pytest.mark.parametrize(("task", "expected"), [
    ("Update requirements.md and README.md", {"requirements.md": "change", "README.md": "change"}),
    ("Create app.js as required by requirements.md", {"app.js": "create"}),
    ("Delete old.js as required by requirements.md", {"old.js": "delete"}),
    ("Rename old.js to app.js as required by requirements.md", {"old.js": "delete", "app.js": "create"}),
])
def test_spec_reference_preserves_explicit_mutation_operations(task, expected):
    from nz_coder.runtime.agent.task_policy import extract_explicit_mutation_operations

    assert extract_explicit_mutation_operations(task) == expected


@pytest.mark.parametrize("task", [
    "Update the files required by requirements.md: app.js and README.md.",
    "Update app.js and, as required by requirements.md, README.md.",
])
def test_required_by_qualifier_preserves_explicit_output_list(task, tmp_path):
    """来源限定语结束后，输出文件列表仍受原修改动词约束。"""
    from nz_coder.runtime.agent.task_policy import extract_explicit_mutation_operations
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    (tmp_path / "requirements.md").write_text("Implement the task board.\n")
    assert extract_explicit_mutation_operations(task) == {
        "app.js": "change", "README.md": "change",
    }
    state = RuntimeState()
    state.apply_current_round_instruction(task, workspace=tmp_path)
    assert state.requested_paths == ["app.js", "README.md"]
    assert [item["expected_artifacts"] for item in state.task_contract["requirements"]] == [
        ["app.js"], ["README.md"],
    ]
    assert [item["path"] for item in state.task_reference_evidence] == ["requirements.md"]


@pytest.mark.parametrize(("task", "expected", "references"), [
    (
        "Update app.js as required by requirements.md:12 and SPEC.md:34.",
        {"app.js": "change"},
        {"requirements.md", "SPEC.md"},
    ),
    (
        "Update app.js, as required by requirements.md, SPEC.md, and DESIGN.md, with the new behavior.",
        {"app.js": "change"},
        {"requirements.md", "SPEC.md", "DESIGN.md"},
    ),
    (
        "Update app.js as required by requirements.md, SPEC.md.",
        {"app.js": "change"},
        {"requirements.md", "SPEC.md"},
    ),
    (
        "Update the files required by requirements.md, SPEC.md: app.js and README.md.",
        {"app.js": "change", "README.md": "change"},
        {"requirements.md", "SPEC.md"},
    ),
    (
        "Create the files required by requirements.md: app.js and README.md.",
        {"app.js": "create", "README.md": "create"},
        {"requirements.md"},
    ),
    (
        "Delete old.js and, as required by requirements.md, obsolete.js.",
        {"old.js": "delete", "obsolete.js": "delete"},
        {"requirements.md"},
    ),
    (
        "Rename old.js, as required by requirements.md, to app.js.",
        {"old.js": "delete", "app.js": "create"},
        {"requirements.md"},
    ),
    (
        "Update app.js and, as required by requirements.md, requirements.md.",
        {"app.js": "change", "requirements.md": "change"},
        {"requirements.md"},
    ),
])
def test_required_by_qualifier_keeps_operations_and_reference_boundaries(task, expected, references):
    from nz_coder.runtime.agent.task_policy import (
        extract_explicit_mutation_operations,
        extract_task_reference_paths,
    )

    assert extract_explicit_mutation_operations(task) == expected
    assert set(extract_task_reference_paths(task)) == references
