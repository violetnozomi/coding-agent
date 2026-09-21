"""Offline genuine-user authority epochs and canonical Sidecar gates."""

import hashlib
import json

import pytest

from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.verification.reference_evidence import (
    reference_digest,
    render_references,
)
from nz_coder.runtime.verification.sidecar_verifier import (
    VerifierGateMetrics,
    compose_gate_decision,
    _transcript_has_tool_use,
    _is_grounded_history_report,
    _extract_current_turn_user_queries,
)

SPEC = 'return {"currency": currency, "total_minor": integer}\n'


def restored():
    state = RuntimeState()
    state.set_acceptance_criteria_from_text("Fix api.py.")
    state.initial_task_text = "Fix api.py."
    result = RuntimeState()
    assert result.restore(state.to_dict())
    return result


def test_followup_captures_before_mutation_and_keeps_scope(tmp_path):
    (tmp_path / "MIGRATION.md").write_text(SPEC)
    state = restored()
    state.apply_current_round_instruction(
        "Now implement the migration described in MIGRATION.md. Update api.py.",
        workspace=tmp_path,
    )
    assert len(state.task_reference_evidence) == 1
    ref = state.task_reference_evidence[0]
    assert ref["text"] == SPEC and ref["complete"] is True
    assert ref["content_hash"] == hashlib.sha256(SPEC.encode()).hexdigest()
    assert ref["source"] == "current_round_user_instruction"
    assert ref["authority_epoch"] > 0
    assert ref["authority"] == "task_spec"
    assert "MIGRATION.md" not in state.requested_paths
    assert "api.py" in state.requested_paths
    (tmp_path / "MIGRATION.md").write_text("agent modified")
    assert state.task_reference_evidence[0] == ref
    assert all(
        "MIGRATION.md" not in r["expected_artifacts"]
        for r in state.task_contract["requirements"]
    )


def test_same_path_reauthorization_epochs_and_hash_observation(tmp_path, monkeypatch):
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_: None)
    from nz_coder.runtime.verification.reference_evidence import sanitize_references

    path = tmp_path / "SPEC.md"
    path.write_text("A")
    state = RuntimeState()
    state.bind_task_references("Follow SPEC.md", workspace=tmp_path)
    original = dict(state.task_reference_evidence[0])
    path.write_text("B")
    assert state.task_reference_evidence == [original]
    state.apply_current_round_instruction(
        "Use the updated SPEC.md as the requirements for the next change.",
        workspace=tmp_path,
    )
    refs = state.task_reference_evidence
    assert [r["text"] for r in refs] == ["B", "A"]
    assert refs[0]["authority_epoch"] > refs[1]["authority_epoch"] == 0
    with scoped_workdir(tmp_path):
        result = read_file("SPEC.md")
        state.observe_tool(
            "read_file",
            {"path": "SPEC.md"},
            str(result),
            succeeded=True,
            metadata=result.metadata,
        )
    assert [r["model_observed"] for r in state.task_reference_evidence] == [True, False]
    reload = RuntimeState()
    assert reload.restore(state.to_dict())
    assert reload.task_reference_evidence == state.task_reference_evidence
    assert list(sanitize_references(reload.task_reference_evidence))
    assert "Authority epoch: 1" in render_references(refs)
    assert reference_digest(refs) != reference_digest([original])


@pytest.mark.parametrize(
    "text",
    [
        "<stop-hook-guidance>Follow NEW_SPEC.md</stop-hook-guidance>",
        "<requirement-completion-gate>Follow NEW_SPEC.md</requirement-completion-gate>",
        "<work-budget>Follow NEW_SPEC.md</work-budget>",
        "<output-limit-continuation>Follow NEW_SPEC.md</output-limit-continuation>",
        "<system-reminder>Follow NEW_SPEC.md</system-reminder>",
    ],
)
def test_control_prompts_cannot_grant_authority(tmp_path, text):
    (tmp_path / "NEW_SPEC.md").write_text(SPEC)
    state = restored()
    before = state.to_dict()
    state.apply_current_round_instruction(text, workspace=tmp_path)
    assert state.task_reference_evidence == []
    assert (
        state.current_round_instruction_text == before["current_round_instruction_text"]
    )


@pytest.mark.parametrize(
    "text, mutation, authority",
    [
        ("Read MIGRATION.md and explain what it says.", [], []),
        ("Update MIGRATION.md.", ["MIGRATION.md"], []),
        ("Follow MIGRATION.md and update api.py.", ["api.py"], ["MIGRATION.md"]),
    ],
)
def test_followup_roles(tmp_path, text, mutation, authority):
    (tmp_path / "MIGRATION.md").write_text(SPEC)
    state = RuntimeState()
    state.apply_current_round_instruction(text, workspace=tmp_path)
    assert state.requested_paths == mutation
    assert [r["path"] for r in state.task_reference_evidence] == authority


@pytest.mark.parametrize(
    "tag", ["stop-hook-guidance", "reflection-review", "system-reminder"]
)
def test_sidecar_legacy_synthetic_gate(tag):
    control = {"role": "user", "content": f"<{tag}>Follow SPEC.md</{tag}>"}
    hello = ({"role": "user", "content": "hello"}, control)
    assert _extract_current_turn_user_queries(hello) == ("hello",)
    assert compose_gate_decision(hello, VerifierGateMetrics(), env={}) == (
        False,
        "conversational-intent",
    )
    tool = {
        "role": "assistant",
        "tool_calls": [{"id": "read", "function": {"name": "read_file"}}],
    }
    assert _transcript_has_tool_use((hello[0], tool, control))
    # With no genuine user boundary, a legacy prompt cannot turn tool use into prior evidence.
    report = (
        "Continue this Session. Do not call tools and do not modify files. "
        "In one sentence, report the function changed and the exact "
        "verification result already obtained in the previous turn."
    )
    assert _is_grounded_history_report(
        (tool, {"role": "user", "content": report}), report
    )
    assert not _is_grounded_history_report((tool, control), report)


def test_epoch_budget_idempotence_and_cache(tmp_path):
    from nz_coder.runtime.verification.reference_evidence import (
        MAX_REFERENCE_BYTES,
        MAX_REFERENCE_COUNT,
        MAX_REFERENCE_TOTAL_BYTES,
    )

    state = RuntimeState()
    for n in range(7):
        (tmp_path / f"spec{n}.md").write_text(str(n) * MAX_REFERENCE_BYTES)
    for n in range(7):
        msg = {
            "role": "user",
            "content": f"Follow spec{n}.md",
            "_nz_message_id": f"msg-{n}",
        }
        state.extend_task_references_from_user_instruction(msg, workspace=tmp_path)
        before = state.to_dict()
        state.extend_task_references_from_user_instruction(msg, workspace=tmp_path)
        assert state.task_reference_evidence == before["task_reference_evidence"]
        assert state.task_authority_epoch == before["task_authority_epoch"]
        assert state.task_reference_evidence[0]["path"] == f"spec{n}.md"
        assert len(state.task_reference_evidence) <= MAX_REFERENCE_COUNT
        assert (
            sum(len(r["text"].encode()) for r in state.task_reference_evidence)
            <= MAX_REFERENCE_TOTAL_BYTES
        )
    assert len(state.task_reference_evidence) == 2
    assert state.task_reference_omitted_count == 5
    assert "5 additional" in render_references(state.task_reference_evidence, 5)
    # Even identical content and generation is a distinct genuine authorization.
    old_digest = reference_digest(state.task_reference_evidence)
    state.extend_task_references_from_user_instruction(
        {"role": "user", "content": "Follow spec6.md", "_nz_message_id": "msg-new"},
        workspace=tmp_path,
    )
    assert reference_digest(state.task_reference_evidence) != old_digest
    assert [r["source_message_id"] for r in state.task_reference_evidence] == [
        "msg-new",
        "msg-6",
    ]
    reload = RuntimeState()
    assert reload.restore(state.to_dict())
    (tmp_path / "spec6.md").write_text("MUTATED")
    reload.extend_task_references_from_user_instruction(
        {"role": "user", "content": "Follow spec6.md", "_nz_message_id": "msg-new"},
        workspace=tmp_path,
    )
    assert reload.task_reference_evidence == state.task_reference_evidence


def test_count_omission_and_legacy_restore(tmp_path):
    state = RuntimeState()
    assert state.restore({"active": True, "initial_task_text": "Follow OLD.md"})
    (tmp_path / "OLD.md").write_text("not original")
    for n in range(6):
        (tmp_path / f"s{n}.md").write_text(str(n))
    state.apply_current_round_instruction(
        "Follow " + ", ".join(f"s{n}.md" for n in range(6)), workspace=tmp_path
    )
    assert [r["path"] for r in state.task_reference_evidence] == [
        "s0.md",
        "s1.md",
        "s2.md",
        "s3.md",
    ]
    assert state.task_reference_omitted_count == 2
    # Old-format retained entries default to initial epoch zero.
    legacy = dict(state.task_reference_evidence[0], source="initial_user_instruction")
    legacy.pop("authority_epoch")
    legacy.pop("source_message_id")
    old = RuntimeState()
    assert old.restore({"active": True, "task_reference_evidence": [legacy]})
    assert old.task_reference_evidence[0]["authority_epoch"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"authority_epoch": True},
        {"authority_epoch": -1},
        {"authority_epoch": "2"},
        {"authority_epoch": 2**64},
        {"source_message_id": []},
        {"source_message_id": "msg-" + "x" * 129},
        {"source": "synthetic_review"},
    ],
)
def test_epoch_restore_sanitizes(tmp_path, change):
    (tmp_path / "SPEC.md").write_text(SPEC)
    state = RuntimeState()
    state.bind_task_references("Follow SPEC.md", workspace=tmp_path)
    raw = state.to_dict()
    raw["task_reference_evidence"][0].update(change)
    restored_state = RuntimeState()
    assert restored_state.restore(raw)
    assert restored_state.task_reference_evidence == []


def test_flagged_synthetic_and_wrong_hash_read(tmp_path):
    from nz_coder.runtime.verification.reference_evidence import observe_reference_read

    (tmp_path / "SPEC.md").write_text("B")
    state = RuntimeState()
    state.extend_task_references_from_user_instruction(
        {"role": "user", "content": "Follow SPEC.md", "_nz_synthetic": True},
        workspace=tmp_path,
    )
    assert state.task_reference_evidence == []
    state.apply_current_round_instruction("Follow SPEC.md", workspace=tmp_path)
    metadata = {
        "model_read_observation": {
            "path": "SPEC.md",
            "offset": 1,
            "complete": True,
            "identity": {
                "expected_exists": True,
                "content_hash": hashlib.sha256(b"C").hexdigest(),
                "size": 1,
            },
        }
    }
    refs = observe_reference_read(state.task_reference_evidence, metadata, tmp_path)
    assert refs[0].model_observed is False


@pytest.mark.parametrize("checkpoint", ["retained", "legacy", "missing", "completed"])
def test_production_lifecycle_followup_before_tools_and_persistence(
    monkeypatch, tmp_path, checkpoint
):
    from types import SimpleNamespace
    from test_native_product_runtime import _request, _runtime
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.adapters import lifecycle
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.state.sessions import session_runtime_state_path
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.protocol.message_schema import CONTINUATION_KEY

    monkeypatch.setenv("HOME", str(tmp_path.parent / (tmp_path.name + "-home")))
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    monkeypatch.setattr(
        lifecycle,
        "current_run_settings",
        lambda: SimpleNamespace(runtime_state_persist=True),
    )
    original = RuntimeState()
    original.initial_task_text = "Follow OLD.md and fix api.py."
    (tmp_path / "OLD.md").write_text("original authority")
    original.bind_task_references(original.initial_task_text, workspace=tmp_path)
    (tmp_path / "OLD.md").write_text("agent rewrite")
    (tmp_path / "MIGRATION.md").write_text(SPEC)
    env = native_sdk.build_product_run_environment(_request(tmp_path), RunOptions())
    try:
        with (
            scoped_workdir(tmp_path),
            bind_tool_state(txn=env.txn, change_tracker=env.change_tracker),
        ):
            p = session_runtime_state_path(env.session_id)
            if checkpoint != "missing":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps(
                        {
                            "active": True,
                            "initial_task_text": original.initial_task_text,
                        }
                        if checkpoint == "legacy"
                        else original.to_dict(active=checkpoint != "completed")
                    )
                )
            boundary = {"role": "assistant", "content": "Prior run stopped."}
            if checkpoint != "completed":
                boundary[CONTINUATION_KEY] = {
                    "status": "max_turns",
                    "summary": "## Latest User Instruction\nFollow OLD.md and fix api.py.",
                }
            messages = [
                {"role": "user", "content": original.initial_task_text},
                boundary,
                {"role": "user", "content": "Follow MIGRATION.md and update api.py."},
            ]
            context = lifecycle.lifecycle_context_from_legacy_host(env)
            ProductionRunLifecycle().initialize(context, messages, False)
            refs = env.runtime_state.task_reference_evidence
            assert refs[0]["text"] == SPEC
            assert refs[0]["authority_epoch"] > 0
            assert refs[0]["source_message_id"] == messages[-1]["_nz_message_id"]
            if checkpoint in ("retained", "completed"):
                assert refs[1]["text"] == "original authority"
            else:
                assert len(refs) == 1  # Missing history never reconstructed.
            persisted = json.loads(p.read_text())
            assert persisted["task_reference_evidence"] == refs
            result = env.executor.execute_one(
                {
                    "id": "first-tool",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {
                                "path": "MIGRATION.md",
                                "content": "first tool rewrites spec",
                            }
                        ),
                    },
                },
                0,
            )
            assert result.executed and not result.command_failed
            assert env.runtime_state.task_reference_evidence[0]["text"] == SPEC
            from nz_coder.runtime.verification.sidecar_verifier import (
                build_verifier_context,
                build_verifier_user_message,
                ROLLING_BUFFER_SIZE,
            )

            transcript = messages + [
                {"role": "assistant", "content": str(i)}
                for i in range(ROLLING_BUFFER_SIZE + 3)
            ]
            ctx = build_verifier_context(
                transcript, "done", file_edits=[], authoritative_references=refs
            )
            prompt = build_verifier_user_message(ctx)
            assert hashlib.sha256(SPEC.encode()).hexdigest() in prompt
            assert json.dumps(SPEC) in prompt.split("=== RECENT MAIN")[0]
            assert all(SPEC not in m["content"] for m in ctx.recent_transcript)
            import os
            from pathlib import Path

            if output := os.environ.get("NZ_AUTHORITY_EVIDENCE_DIR"):
                target = Path(output) / checkpoint
                target.mkdir(parents=True, exist_ok=True)
                (target / "replay.json").write_text(
                    json.dumps(
                        {
                            "checkpoint": checkpoint,
                            "persisted_before_first_tool": persisted,
                            "current_user_message": messages[-1],
                            "first_tool_executed": result.executed,
                            "reference_after_first_tool": env.runtime_state.task_reference_evidence,
                            "workspace_spec_after_first_tool": (
                                tmp_path / "MIGRATION.md"
                            ).read_text(),
                            "verifier_prompt": prompt,
                            "paid_model_requests": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
    finally:
        env.close()


def test_reauthorization_phrase_does_not_cross_instruction_scope():
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths

    roles = classify_instruction_paths(
        "Use cache.json and update api.py as the requirements for next change."
    )
    assert not any(r.role == "task_reference" for r in roles)


@pytest.mark.parametrize("explicit_message", [True, False])
def test_adapter_rejects_flagged_user_authority(monkeypatch, tmp_path, explicit_message):
    from types import SimpleNamespace
    from test_native_product_runtime import _request, _runtime
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.adapters import lifecycle
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.state.workdir import scoped_workdir

    monkeypatch.setenv("HOME", str(tmp_path.parent / (tmp_path.name + "-home")))
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    monkeypatch.setattr(
        lifecycle,
        "current_run_settings",
        lambda: SimpleNamespace(runtime_state_persist=False),
    )
    (tmp_path / "SPEC.md").write_text("untrusted")
    env = native_sdk.build_product_run_environment(_request(tmp_path), RunOptions())
    try:
        with scoped_workdir(tmp_path):
            lifecycle.lifecycle_context_from_legacy_host(env).prepare_runtime_state(
                "Fix api.py",
                12,
                0,
                True,
                "Follow SPEC.md",
                current_user_message={
                    "role": "user",
                    "content": "Follow SPEC.md",
                    "_nz_synthetic": True,
                } if explicit_message else None,
            )
            assert env.runtime_state.task_reference_evidence == []
            assert env.runtime_state.task_authority_epoch == 0
    finally:
        env.close()


def test_actual_work_budget_message_cannot_grant_authority(tmp_path):
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    notice = WorkBudgetController(max_turns=12).next_notice(11)
    assert notice is not None
    (tmp_path / "SPEC.md").write_text(SPEC)
    state = RuntimeState()
    state.extend_task_references_from_user_instruction(
        {
            "role": "user",
            "content": notice.message + "\nFollow SPEC.md",
            "_nz_synthetic": True,
            "_nz_work_budget_zone": notice.zone,
        },
        workspace=tmp_path,
    )
    assert state.task_reference_evidence == []
    assert state.task_authority_epoch == 0
