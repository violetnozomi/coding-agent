"""Offline observations of C closure; not a new semantic policy specification."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract
from nz_coder.runtime.verification.completion_gate import CompletionGate

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/evaluation/fixtures/agent_core_diagnostic_v1/C_long_horizon"
HISTORY = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder"
COMMAND = "python -m pytest -q tests"


def record(name, value):
    destination = os.environ.get("NZ_C_CLOSURE_AUDIT_OUTPUT")
    if destination:
        path = Path(destination) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def contract(kind="behavior", mode="semantic", artifacts=()):
    return TaskContract.from_dict({"requirements": [{
        "id": "R1", "description": "Implement all required behavior.",
        "kind": kind, "satisfaction_mode": mode,
        "expected_artifacts": list(artifacts),
    }], "acceptance_commands": [COMMAND]})


@pytest.mark.parametrize("kind,mode,artifacts,expected", [
    ("behavior", "semantic", (), "satisfied"),
    ("behavior", "mixed", (), "satisfied"),
    ("behavior", "deterministic", (), "satisfied"),
    ("compatibility", "semantic", (), "candidate"),
    ("docs", "mixed", ("README.md",), "satisfied"),
    ("test", "mixed", ("tests/test_api.py",), "satisfied"),
    ("artifact", "deterministic", ("api.py",), "satisfied"),
    ("verification", "semantic", (), "satisfied"),
])
def test_observed_satisfaction_mode_matrix(kind, mode, artifacts, expected):
    ledger = RequirementLedger.from_contract(contract(kind, mode, artifacts))
    ledger.observe_mutation(1, list(artifacts) or ["api.py"])
    before = ledger.to_dict()
    ledger.observe_verification(1, command=COMMAND, passed=True, acceptance=True)
    assert ledger.status("R1") == expected
    record(f"ledger/matrix-{kind}-{mode}.json", {"before": before, "after": ledger.to_dict()})


def test_semantic_behavior_project_test_closure_is_current_policy():
    """Observation, not an invented RED requiring all semantic modes to use LLM."""
    data = contract().to_dict()
    data["requirements"].append({"id": "R2", "kind": "verification",
                                 "description": "Pass pytest", "satisfaction_mode": "semantic"})
    ledger = RequirementLedger.from_contract(TaskContract.from_dict(data))
    ledger.observe_mutation(1, ["api.py"])
    ledger.observe_verification(1, command=COMMAND, passed=True, acceptance=False)
    assert [item.status for item in ledger.items.values()] == ["pending", "pending"]
    before = ledger.to_dict()
    ledger.observe_verification(1, command=COMMAND, passed=True, acceptance=True)
    assert [item.status for item in ledger.items.values()] == ["satisfied", "satisfied"]
    assert CompletionGate().evaluate(ledger, mutation_generation=1).ready
    record("ledger/verification-promotion.json", {"generic": before, "exact": ledger.to_dict()})


def test_narrative_only_review_can_close_compatibility_at_same_generation():
    ledger = RequirementLedger.from_contract(contract("compatibility"))
    ledger.observe_mutation(1, ["api.py"])
    ledger.observe_verification(1, command=COMMAND, passed=True, acceptance=True)
    ledger.observe_semantic_review(1, accepted=False, fingerprint="first-report")
    rejected = ledger.to_dict()
    assert ledger.status("R1") == "candidate"
    assert not CompletionGate().evaluate(ledger, mutation_generation=1).ready
    ledger.observe_semantic_review(1, accepted=True, fingerprint="revised-report")
    assert ledger.status("R1") == "satisfied"
    assert CompletionGate().evaluate(ledger, mutation_generation=1).ready
    assert ledger.latest_generation == ledger.latest_verification_generation == 1
    record("ledger/narrative-only-review.json", {"rejected": rejected, "accepted": ledger.to_dict(),
           "scope": "controlled verdict transition; no claim a model must accept or detect the defect"})


@pytest.mark.parametrize("planner", ["disabled", "unavailable", "coarse", "detailed"])
def test_c_production_bootstrap_before_any_provider(monkeypatch, tmp_path, planner):
    import shutil
    import socket
    from test_native_product_runtime import _request, _runtime
    from nz_coder.runtime.adapters import lifecycle
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import loop, native_sdk
    from nz_coder.state.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE / "workspace", workspace)
    task = (FIXTURE / "task.md").read_text()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    monkeypatch.setattr(lifecycle, "current_run_settings", lambda: SimpleNamespace(runtime_state_persist=False))
    monkeypatch.setattr(loop, "current_run_settings", lambda: SimpleNamespace(
        planning_enabled=planner != "disabled", runtime_state_persist=False))
    monkeypatch.setattr(loop, "strict_local_tools", lambda: False)

    def no_network(*args, **kwargs):
        raise AssertionError("audit attempted network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    env = native_sdk.build_product_run_environment(_request(workspace), RunOptions())
    calls = []

    async def controlled_planner(text):
        calls.append(text)
        if planner == "unavailable":
            raise RuntimeError("controlled planner unavailable")
        payload = contract().to_dict()
        if planner == "detailed":
            payload["requirements"] += [{
                "id": "R2", "description": "Validate direct Config writes before changing an existing destination.",
                "kind": "behavior", "satisfaction_mode": "semantic",
                "expected_artifacts": ["configkit/config/writer.py"],
                "required_evidence": ["semantic_review"],
            }]
        return json.dumps(payload)

    monkeypatch.setattr(env, "_call_planning_llm_async", controlled_planner)
    try:
        with scoped_workdir(workspace):
            lifecycle.lifecycle_context_from_legacy_host(env).prepare_runtime_state(task, 24, 0)
            env.runtime_state.task_mode = "feature"
            asyncio.run(env._maybe_generate_plan([{"role": "user", "content": task}]))
            state = env.runtime_state
            requirements = state.task_contract["requirements"]
            if planner in {"disabled", "unavailable"}:
                assert [r["kind"] for r in requirements] == ["behavior", "compatibility", "verification"]
                historical = json.loads((HISTORY / "final-state.json").read_text())["state"]
                assert state.task_contract == historical["task_contract"]
            else:
                assert len(requirements) == (2 if planner == "detailed" else 1)
                if planner == "detailed":
                    assert requirements[1]["required_evidence"] == ["semantic_review"]
            assert len(calls) == (0 if planner == "disabled" else 1)
            reference = state.task_reference_evidence[0]
            assert reference["text"] == (workspace / "CONFIG_SPEC.md").read_text()
            assert not reference["model_observed"]
            assert "CONFIG_SPEC.md" not in state.requested_paths
            record(f"contract/bootstrap-{planner}.json", {
                "task_contract": state.task_contract, "ledger": state.requirement_ledger,
                "references": state.task_reference_evidence, "controlled_planner_calls": len(calls),
                "provider_calls": 0,
            })
    finally:
        env.close()


def test_frozen_verifier_authority_is_complete_but_not_clause_inventory():
    from nz_coder.runtime.verification.sidecar_verifier import VERIFIER_SYSTEM_PROMPT
    reviews = json.loads((HISTORY / "semantic-review.json").read_text())
    spec = (FIXTURE / "workspace/CONFIG_SPEC.md").read_text()
    for review in reviews:
        messages = review["visible_input"]
        assert messages[0] == {"role": "system", "content": VERIFIER_SYSTEM_PROMPT}
        visible = messages[1]["content"]
        assert json.dumps(spec, ensure_ascii=False) in visible
        assert hashlib.sha256(spec.encode()).hexdigest() in visible
        assert "Complete: true" in visible
        assert "Requirement R1 [behavior]" in visible
        assert "Requirement R3 [verification]" in visible
        assert "Requirement R4" not in visible
        assert "This fact proves execution only" in visible


def test_production_change_tracker_diff_is_attributed_per_file(tmp_path):
    from nz_coder.state.changes import ChangeTracker
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.runtime.verification.sidecar_verifier import (
        _bounded_diff_hints, _compatibility_delta_evidence,
        build_verifier_context, build_verifier_user_message,
        SidecarVerifierHook,
    )
    from nz_coder.runtime.verification.hooks import StopHookContext
    with scoped_workdir(tmp_path):
        tracker = ChangeTracker("closure-audit", change_dir=tmp_path / ".changes")
        for path, text in [("parser.py", "PARSER_SENTINEL = 1\n"),
                           ("writer.py", "WRITER_SENTINEL = 2\n")]:
            tracker.record_before(path, True, "old = 0\n")
            (tmp_path / path).write_text(text)
            tracker.record_after(path, True, text)
        diff = tracker.render_current_diff()
        hints = _bounded_diff_hints(diff, tracker.current_changed_paths())
        hook = SidecarVerifierHook(SimpleNamespace(change_tracker=tracker, workdir=tmp_path),
                                  resolved=None, env={})
        packet, _, _ = hook._evidence(StopHookContext(
            transcript=({"role": "user", "content": "Update both modules"},),
            last_assistant_text="Done", runtime_state={}))
        production_visible = build_verifier_user_message(packet)
    record("verifier/production-diff-projection.json", {"diff": diff.replace(str(tmp_path), "<WORKSPACE>"),
           "hints": {k: v.replace(str(tmp_path), "<WORKSPACE>") for k, v in hints.items()}})
    assert "WRITER_SENTINEL" not in hints["parser.py"]
    assert "PARSER_SENTINEL" not in hints["writer.py"]
    assert "WRITER_SENTINEL" in hints["writer.py"]
    delta = _compatibility_delta_evidence(diff, ["writer.py"])
    assert "WRITER_SENTINEL" in delta
    assert "PARSER_SENTINEL" not in delta
    context = build_verifier_context([{"role": "user", "content": "Update both modules"}],
        "Done", file_edits=[{"path": p, "diff_hint": h} for p, h in hints.items()])
    visible = build_verifier_user_message(context)
    assert visible.count("WRITER_SENTINEL") == 1
    assert visible.count("PARSER_SENTINEL") == 1
    assert production_visible.count("WRITER_SENTINEL") == 1
    assert production_visible.count("PARSER_SENTINEL") == 1


def test_c_frozen_diff_projection_does_not_repeat_entire_changeset():
    from nz_coder.runtime.verification.sidecar_verifier import _bounded_diff_hints, _rank_semantic_paths
    state = json.loads((HISTORY / "final-state.json").read_text())["state"]
    diff = (HISTORY / "workspace.diff").read_text()
    paths = _rank_semantic_paths(state["changed_files"], state)
    hints = _bounded_diff_hints(diff, paths, max_each=5000, max_total=12000)
    record("verifier/c-diff-hints.json", hints)
    assert "configkit/config/writer.py" in hints
    assert "def to_dict(config):" in hints["configkit/config/writer.py"]
    assert "def validate(data):" not in hints["configkit/config/writer.py"]
    assert sum(len(h) for h in hints.values()) <= 12000


@pytest.mark.parametrize("style", ["git", "plain", "tracker"])
def test_diff_formats_keep_file_boundaries_and_literal_markdown(style):
    from nz_coder.runtime.verification.sidecar_verifier import _bounded_diff_hints
    first = "--- a/first.md\n+++ b/first.md\n@@ -1 +1 @@\n-old\n+## fake.py\n"
    second = "--- a/second.py\n+++ b/second.py\n@@ -1 +1 @@\n-old\n+SECOND_SENTINEL\n"
    if style == "git":
        first = "diff --git a/first.md b/first.md\n" + first
        second = "diff --git a/second.py b/second.py\n" + second
    if style == "tracker":
        first = "## first.md\n" + first
        second = "## second.py\n" + second
    hints = _bounded_diff_hints(first + second, ["first.md", "second.py"])
    assert "SECOND_SENTINEL" not in hints["first.md"]
    assert "+## fake.py" in hints["first.md"]
    assert "SECOND_SENTINEL" in hints["second.py"]
