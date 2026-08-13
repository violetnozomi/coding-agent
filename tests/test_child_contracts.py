"""Contracts for child workflow evidence, verification, and summaries."""
from __future__ import annotations

import pytest


def test_evidence_refs_fail_fast_on_unknown_prefix_and_empty_task():
    from nz_coder.runtime.child_contracts import normalize_evidence_refs

    with pytest.raises(ValueError, match="unsupported evidence ref"):
        normalize_evidence_refs(["baseline"])
    with pytest.raises(ValueError, match="requires a value"):
        normalize_evidence_refs(["task_id:"])


def test_evidence_briefing_resolves_file_finding_and_prior_task(tmp_path):
    from nz_coder.runtime.child_contracts import build_evidence_briefing

    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    briefing = build_evidence_briefing(
        ["file:app.py", "finding:check VALUE", "task_id:child-1"],
        workspace=tmp_path,
        load_task_state=lambda task_id: {
            "child_result": {"final_text": f"result from {task_id}"}
        },
    )

    assert "VALUE = 1" in briefing
    assert "check VALUE" in briefing
    assert "result from child-1" in briefing
    assert "untrusted context" in briefing


def test_evidence_file_path_cannot_escape_workspace(tmp_path):
    from nz_coder.runtime.child_contracts import build_evidence_briefing

    with pytest.raises(ValueError, match="escapes workspace"):
        build_evidence_briefing(
            ["file:../secret.txt"],
            workspace=tmp_path,
            load_task_state=lambda _task_id: {},
        )


def test_verification_contract_rejects_unsafe_paths_and_unknown_fields():
    from nz_coder.runtime.child_contracts import normalize_verification_contract

    with pytest.raises(ValueError, match="unsafe path"):
        normalize_verification_contract({
            "required_changed_paths": ["../outside.py"],
        })
    with pytest.raises(ValueError, match="unsupported verification field"):
        normalize_verification_contract({"magic_score": 1})


def test_verification_evaluates_mutation_read_and_final_text_evidence():
    from nz_coder.runtime.child_contracts import evaluate_child_verification

    state = {
        "changed_files": ["src/app.py"],
        "messages": [{
            "_nz_parts": [{
                "type": "tool",
                "tool": "read_file",
                "state": {
                    "status": "completed",
                    "input": {"path": "tests/test_app.py"},
                },
            }],
        }],
    }
    result = evaluate_child_verification(
        {
            "enforcement": "hard",
            "requires_mutation": True,
            "required_changed_paths": ["src/app.py"],
            "required_read_paths": ["tests/test_app.py"],
            "min_final_text_chars": 1000,
        },
        state=state,
        final_text="done",
    )

    assert result["ok"] is True
    assert result["mutation_evidence"] is True
    assert result["read_paths"] == ["tests/test_app.py"]


def test_verification_rejects_preparatory_non_terminal_text():
    from nz_coder.runtime.child_contracts import evaluate_child_verification

    result = evaluate_child_verification(
        {
            "enforcement": "hard",
            "requires_mutation": True,
            "reject_preparatory_final_text": True,
        },
        state={"changed_files": [], "messages": []},
        final_text="I will inspect the parser next.",
    )

    assert result["ok"] is False
    assert len(result["reasons"]) == 2


def test_verification_reads_evidence_from_session_transcript_not_task_state():
    from nz_coder.runtime.child_contracts import evaluate_child_verification

    transcript = [{
        "role": "assistant",
        "content": "",
        "_nz_parts": [{
            "type": "tool",
            "tool": "read_file",
            "state": {
                "status": "completed",
                "input": {"path": "src/native.py"},
            },
        }],
    }]

    result = evaluate_child_verification(
        {"required_read_paths": ["src/native.py"]},
        state={"changed_files": []},
        messages=transcript,
        final_text="reviewed",
    )

    assert result["ok"] is True
    assert result["read_paths"] == ["src/native.py"]


def test_presentation_excerpt_is_bounded_and_truthfully_labeled():
    from nz_coder.runtime.child_contracts import presentation_excerpt

    summary, kind = presentation_excerpt("word " * 500, max_chars=80)

    assert kind == "excerpt"
    assert len(summary) <= 80
    assert summary.endswith("…")
