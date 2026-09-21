"""Original task authority survives conversation eviction; all execution offline."""

import hashlib
import json

import pytest

from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.verification.sidecar_verifier import (
    ROLLING_BUFFER_SIZE,
    build_verifier_context,
    build_verifier_user_message,
)

SPEC = 'web.checkout returns {"currency": currency, "total_minor": integer}.\n'
TASK = "Implement the behavior described in REQUIREMENTS.md. Fix api.py and run python -m pytest -q tests."


def test_production_retains_original_reference_outside_rolling_history(
    monkeypatch, tmp_path
):
    from test_requirement_scope_runtime import _run

    (tmp_path / "REQUIREMENTS.md").write_text(SPEC)
    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    for i in range(ROLLING_BUFFER_SIZE + 3):
        (tmp_path / f"context{i}.txt").write_text(f"ordinary context {i}\n")
    actions = [[("read_file", {"path": "REQUIREMENTS.md"})]]
    actions += [
        [("read_file", {"path": f"context{i}.txt"})]
        for i in range(ROLLING_BUFFER_SIZE + 3)
    ]
    actions += [
        [("write_file", {"path": "api.py", "content": "def answer(): return 1\n"})],
        [("bash", {"command": "python -m pytest -q tests"})],
        "Implemented and verified; no external integrations exercised.",
    ]
    _, state, _, requests, reviews = _run(
        monkeypatch, tmp_path, TASK, actions, label="reference-retention"
    )
    assert reviews
    message = reviews[0]["messages"][1]["content"]
    recent = message.split("=== RECENT MAIN AGENT TRANSCRIPT ===")[1].split(
        "=== FILE EDITS"
    )[0]
    assert SPEC.strip() not in recent
    assert "=== AUTHORITATIVE TASK REFERENCES ===" in message
    authority = message.split("=== AUTHORITATIVE TASK REFERENCES ===")[1].split(
        "=== RECENT"
    )[0]
    assert json.dumps(SPEC, ensure_ascii=False) in authority
    assert "Complete: true" in authority
    assert hashlib.sha256(SPEC.encode()).hexdigest() in authority
    assert "Observed by main agent: true" in authority
    assert "REQUIREMENTS.md" not in state["requested_paths"]
    assert all(
        "REQUIREMENTS.md" not in r["expected_artifacts"]
        for r in state["task_contract"]["requirements"]
    )
    assert requests[0]


def test_reference_bootstrap_preserves_provenance(tmp_path):
    from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts

    (tmp_path / "REQUIREMENTS.md").write_text(SPEC)
    ref = next(
        r
        for r in resolve_bootstrap_artifacts(TASK, workspace=tmp_path).artifacts
        if r.path == "REQUIREMENTS.md"
    )
    assert ref.role == "task_reference"
    assert ref.authority == "task_spec"
    assert not ref.required


def test_review_feedback_is_not_new_user_authority():
    from nz_coder.runtime.verification.sidecar_verifier import REVISE_RETROSPECTIVE

    assert "ground truth" not in REVISE_RETROSPECTIVE
    assert "not as new user authority" in REVISE_RETROSPECTIVE


@pytest.mark.parametrize(
    "task, expected",
    [
        (
            "Read README.md and inspect api.py",
            [("README.md", "context", ""), ("api.py", "context", "")],
        ),
        (
            "Implement behavior described in REQUIREMENTS.md",
            [("REQUIREMENTS.md", "task_reference", "task_spec")],
        ),
        (
            "According to SPEC.md, change api.py",
            [("SPEC.md", "task_reference", "task_spec"), ("api.py", "mutation", "")],
        ),
        ("Follow SPEC.md", [("SPEC.md", "task_reference", "task_spec")]),
        (
            "Implement as specified in CONTRACT.md",
            [("CONTRACT.md", "task_reference", "task_spec")],
        ),
        (
            "按照 REQUIREMENTS.md 实现功能",
            [("REQUIREMENTS.md", "task_reference", "task_spec")],
        ),
        (
            "根据 SPEC.md 修改 api.py",
            [("SPEC.md", "task_reference", "task_spec"), ("api.py", "mutation", "")],
        ),
        ("遵循 规格.md 中的要求", [("规格.md", "task_reference", "task_spec")]),
        ("Update REQUIREMENTS.md", [("REQUIREMENTS.md", "mutation", "")]),
        (
            "Read REQUIREMENTS.md and fix api.py",
            [("REQUIREMENTS.md", "context", ""), ("api.py", "mutation", "")],
        ),
        (
            "Run node --test literal.test.cjs",
            [("literal.test.cjs", "verification", "")],
        ),
        (
            "Modify literal.test.cjs and run node --test literal.test.cjs",
            [
                ("literal.test.cjs", "mutation", ""),
                ("literal.test.cjs", "verification", ""),
            ],
        ),
        (
            "Do not follow SPEC.md; fix api.py",
            [("SPEC.md", "context", ""), ("api.py", "mutation", "")],
        ),
    ],
)
def test_shared_path_roles(task, expected):
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths

    assert [
        (r.path, r.role, r.authority) for r in classify_instruction_paths(task)
    ] == expected


def _bound(tmp_path, task=TASK):
    (tmp_path / "REQUIREMENTS.md").write_text(SPEC)
    state = RuntimeState()
    state.bind_task_references(task, workspace=tmp_path)
    return state


def test_original_is_immutable_and_observation_uses_metadata(monkeypatch, tmp_path):
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_: None)
    state = _bound(tmp_path)
    with scoped_workdir(tmp_path):
        result = read_file("REQUIREMENTS.md")
        state.observe_tool(
            "read_file",
            {"path": "REQUIREMENTS.md"},
            "unrelated display",
            succeeded=True,
            metadata=result.metadata,
        )
    assert state.task_reference_evidence[0]["model_observed"] is True
    (tmp_path / "REQUIREMENTS.md").write_text("Changed rules")
    state.bind_task_references(TASK, workspace=tmp_path)
    item = state.task_reference_evidence[0]
    assert item["text"] == SPEC
    assert item["content_hash"] == hashlib.sha256(SPEC.encode()).hexdigest()


def test_modified_read_or_forged_output_does_not_observe_original(
    monkeypatch, tmp_path
):
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_: None)
    state = _bound(tmp_path)
    (tmp_path / "REQUIREMENTS.md").write_text("Changed rules")
    with scoped_workdir(tmp_path):
        result = read_file("REQUIREMENTS.md")
        state.observe_tool(
            "read_file",
            {"path": "REQUIREMENTS.md"},
            SPEC,
            succeeded=True,
            metadata=result.metadata,
        )
    assert state.task_reference_evidence[0]["model_observed"] is False
    assert state.task_reference_evidence[0]["text"] == SPEC


def test_budgets_and_restore(tmp_path):
    from nz_coder.runtime.verification.reference_evidence import (
        MAX_REFERENCE_COUNT,
        MAX_REFERENCE_BYTES,
        MAX_REFERENCE_TOTAL_BYTES,
    )

    paths = [f"spec{i}.md" for i in range(MAX_REFERENCE_COUNT + 3)]
    for name in paths:
        (tmp_path / name).write_text("x" * MAX_REFERENCE_BYTES)
    state = RuntimeState()
    state.bind_task_references("Follow " + ", ".join(paths), workspace=tmp_path)
    refs = state.task_reference_evidence
    assert len(refs) == MAX_REFERENCE_COUNT
    assert [r["path"] for r in refs] == paths[:MAX_REFERENCE_COUNT]
    assert sum(len(r["text"].encode()) for r in refs) <= MAX_REFERENCE_TOTAL_BYTES
    assert state.task_reference_omitted_count == 3
    assert any(r["complete"] is False for r in refs)
    restored = RuntimeState()
    assert restored.restore(state.to_dict())
    assert restored.task_reference_evidence == refs
    assert restored.task_reference_omitted_count == 3
    old = RuntimeState()
    assert old.restore({"active": True})
    assert old.task_reference_evidence == []


def test_oversized_reference_is_explicitly_incomplete(tmp_path):
    from nz_coder.runtime.verification.reference_evidence import MAX_REFERENCE_BYTES

    (tmp_path / "SPEC.md").write_text("界" * MAX_REFERENCE_BYTES)
    state = RuntimeState()
    state.bind_task_references("Follow SPEC.md", workspace=tmp_path)
    ref = state.task_reference_evidence[0]
    assert ref["complete"] is False
    assert len(ref["text"].encode()) <= MAX_REFERENCE_BYTES
    assert ref["capture_status"] == "size_limit"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/tmp/outside.md",
        ".git/config.md",
        "credentials.json",
        "escape.md",
    ],
)
def test_safe_capture_never_reads_outside_or_private(tmp_path, path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("PRIVATE_SENTINEL")
    (tmp_path / "escape.md").symlink_to(outside)
    state = RuntimeState()
    state.bind_task_references("Follow " + path, workspace=tmp_path)
    assert "PRIVATE_SENTINEL" not in json.dumps(state.task_reference_evidence)
    assert all(not r["complete"] for r in state.task_reference_evidence)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "../escape.md"},
        {"authority": "system"},
        {"source": "model"},
        {"text": "forged text"},
        {"content_hash": "bad"},
        {"complete": "true"},
        {"model_observed": 1},
        {"captured_generation": -1},
        {"captured_generation": True},
    ],
)
def test_restore_rejects_corrupt_evidence(tmp_path, change):
    state = _bound(tmp_path)
    snapshot = state.to_dict()
    snapshot["task_reference_evidence"][0].update(change)
    restored = RuntimeState()
    assert restored.restore(snapshot)
    assert restored.task_reference_evidence == []


def test_cache_identity_includes_references_and_does_not_embed_text(tmp_path):
    from dataclasses import replace
    from nz_coder.runtime.verification.sidecar_verifier import (
        SidecarVerifierHook,
        VerifierGateMetrics,
    )
    from nz_coder.runtime.verification.reference_evidence import sanitize_references

    state = _bound(tmp_path)
    refs = sanitize_references(state.task_reference_evidence)
    context = build_verifier_context(
        [{"role": "user", "content": TASK}],
        "done",
        file_edits=[],
        authoritative_references=refs,
    )
    original = SidecarVerifierHook._accepted_cache_key(
        context, VerifierGateMetrics(), {}
    )
    for change in (
        {"model_observed": True},
        {"complete": False},
        {"captured_generation": 1},
    ):
        modified = replace(
            context, authoritative_references=(replace(refs[0], **change),)
        )
        assert (
            SidecarVerifierHook._accepted_cache_key(modified, VerifierGateMetrics(), {})
            != original
        )
    assert SPEC not in str(original)


def test_reference_versions_reach_production_review_and_revision(monkeypatch, tmp_path):
    from test_requirement_scope_runtime import _run

    (tmp_path / "REQUIREMENTS.md").write_text(SPEC)
    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    actions = [
        [
            (
                "write_file",
                {"path": "REQUIREMENTS.md", "content": "Agent changed the rules"},
            )
        ],
        [("read_file", {"path": "REQUIREMENTS.md"})],
        [("write_file", {"path": "api.py", "content": "def answer(): return 1\n"})],
        [("bash", {"command": "python -m pytest -q tests"})],
        "Verified one test, no external integration exercised.",
    ]
    _, state, _, requests, reviews = _run(
        monkeypatch,
        tmp_path,
        TASK,
        actions,
        semantic=("revise", "accept"),
        label="spec-self-modification",
    )
    ref = state["task_reference_evidence"][0]
    assert ref["text"] == SPEC and not ref["model_observed"]
    assert ref["captured_generation"] == 0
    for request in reviews:
        authority = (
            request["messages"][1]["content"]
            .split("=== AUTHORITATIVE TASK REFERENCES ===")[1]
            .split("=== RECENT")[0]
        )
        assert json.dumps(SPEC) in authority
        assert "Agent changed the rules" not in authority
    followups = [
        m
        for m in requests[-1]["messages"]
        if m.get("role") == "user" and "<stop-hook-guidance>" in str(m.get("content"))
    ]
    assert followups
    assert "not as new user authority" in followups[0]["content"]
    assert json.dumps(SPEC) in followups[0]["content"]
    assert "Observed by main agent: false" in followups[0]["content"]


def test_synthetic_messages_never_grant_authority(tmp_path):
    from nz_coder.runtime.execution.run_lifecycle import last_user_text

    (tmp_path / "SPEC.md").write_text("not user authority")
    messages = [
        {"role": "user", "content": "Read README.md and inspect api.py"},
        {"role": "assistant", "content": "investigating"},
        {"role": "user", "content": "Follow SPEC.md", "_nz_synthetic": True},
        {
            "role": "user",
            "content": "<stop-hook-guidance>Follow SPEC.md</stop-hook-guidance>",
        },
    ]
    state = RuntimeState()
    state.bind_task_references(last_user_text(messages), workspace=tmp_path)
    assert state.task_reference_evidence == []
    context = build_verifier_context(messages, "done", file_edits=[])
    assert context.current_turn_user_queries == ("Read README.md and inspect api.py",)
    assert not context.authoritative_references
    assert (
        "[SYNTHETIC REVIEW/CONTROL GUIDANCE; NOT USER AUTHORITY]"
        in build_verifier_user_message(context)
    )
    state.bind_task_references("Follow SPEC.md", workspace=tmp_path)
    assert state.task_reference_evidence == []


def test_resume_never_recaptures_modified_authority(tmp_path):
    original = _bound(tmp_path)
    original.task_reference_evidence[0]["model_observed"] = True
    checkpoint = tmp_path / "checkpoint.json"
    original.save(checkpoint)
    (tmp_path / "REQUIREMENTS.md").write_text("Agent replacement")
    restored = RuntimeState()
    assert restored.load(checkpoint)
    restored.bind_task_references(TASK, workspace=tmp_path)
    assert restored.task_reference_evidence == original.task_reference_evidence
    legacy = RuntimeState()
    assert legacy.restore({"active": True, "initial_task_text": TASK})
    legacy.bind_task_references(TASK, workspace=tmp_path)
    assert legacy.task_reference_evidence == []


@pytest.mark.parametrize(
    "kind", ["failed", "partial", "no_metadata", "wrong_path", "wrong_hash"]
)
def test_read_observation_fail_closed(monkeypatch, tmp_path, kind):
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_: None)
    state = _bound(tmp_path)
    with scoped_workdir(tmp_path):
        result = read_file("REQUIREMENTS.md")
        metadata = result.metadata
        if kind == "partial":
            metadata["model_read_observation"]["complete"] = False
        if kind == "wrong_path":
            metadata["model_read_observation"]["path"] = "other.md"
        if kind == "wrong_hash":
            metadata["model_read_observation"]["identity"]["content_hash"] = "0" * 64
        state.observe_tool(
            "read_file",
            {"path": "REQUIREMENTS.md"},
            str(result),
            succeeded=kind != "failed",
            metadata=None if kind == "no_metadata" else metadata,
        )
    assert state.task_reference_evidence[0]["model_observed"] is False


def test_hash_and_budgets_reject_oversized_or_invalid_snapshot(tmp_path):
    from nz_coder.runtime.verification.reference_evidence import MAX_REFERENCE_BYTES

    state = _bound(tmp_path)
    for raw in (None, "x", {}, 42, [{"path": 3}]):
        snapshot = state.to_dict()
        snapshot["task_reference_evidence"] = raw
        restored = RuntimeState()
        assert restored.restore(snapshot)
        assert restored.task_reference_evidence == []
    snapshot = state.to_dict()
    item = snapshot["task_reference_evidence"][0]
    item["text"] = "x" * (MAX_REFERENCE_BYTES + 1)
    item["content_hash"] = hashlib.sha256(item["text"].encode()).hexdigest()
    restored = RuntimeState()
    assert restored.restore(snapshot)
    assert restored.task_reference_evidence == []


def test_reference_and_mutation_dual_role_preserved(tmp_path):
    from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts
    from nz_coder.runtime.agent.task_contract import derive_task_contract
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths

    task = "Follow SPEC.md and update SPEC.md. Run python -m pytest -q tests."
    (tmp_path / "SPEC.md").write_text(SPEC)
    roles = classify_instruction_paths(task)
    assert {(r.role, r.authority) for r in roles if r.path == "SPEC.md"} == {
        ("mutation", ""),
        ("task_reference", "task_spec"),
    }
    ref = next(
        r
        for r in resolve_bootstrap_artifacts(task, workspace=tmp_path).artifacts
        if r.path == "SPEC.md"
    )
    assert ref.required and ref.authority == "task_spec"
    contract = derive_task_contract(
        task, acceptance_command="python -m pytest -q tests", workspace=tmp_path
    )
    assert any("SPEC.md" in r.expected_artifacts for r in contract.requirements)


def test_authority_precedence_preserves_compatibility_rules():
    from nz_coder.runtime.verification.sidecar_verifier import (
        VERIFIER_SYSTEM_PROMPT,
        SEMANTIC_CONTRACT_CERTIFICATION,
    )

    assert (
        "cannot override an explicit requested semantic change"
        in VERIFIER_SYSTEM_PROMPT
    )
    assert (
        "unless the user requested the semantic change"
        in SEMANTIC_CONTRACT_CERTIFICATION
    )
    assert "filename alone grants no authority" in VERIFIER_SYSTEM_PROMPT


def test_count_budget_is_detectable_over_old_path_extraction_limit(tmp_path):
    state = RuntimeState()
    state.bind_task_references(
        "Follow " + ", ".join(f"s{i}.md" for i in range(120)), workspace=tmp_path
    )
    assert len(state.task_reference_evidence) == 4
    assert state.task_reference_omitted_count == 116


def test_bootstrap_precedes_even_first_file_mutation(monkeypatch, tmp_path):
    # Production self-modification regression above proves capture at G0, before
    # any read, and canonical read of changed hash leaves original unobserved.
    from test_requirement_scope_runtime import _money, _money_actions, _run

    fixture = _money(tmp_path)
    _, state, _, _, reviews = _run(
        monkeypatch,
        tmp_path,
        fixture["task"],
        _money_actions(fixture),
        label="money-authority",
    )
    ref = state["task_reference_evidence"][0]
    assert ref["text"] == fixture["initial"]["REQUIREMENTS.md"]
    assert ref["complete"] and ref["model_observed"]
    assert '"total_minor"' in ref["text"]
    assert ref["captured_generation"] == 0
    message = reviews[0]["messages"][1]["content"]
    assert json.dumps(ref["text"], ensure_ascii=False) in message
    assert all(
        "REQUIREMENTS.md" not in r["expected_artifacts"]
        for r in state["task_contract"]["requirements"]
    )


def test_missing_original_capture_never_retries_mutable_file(tmp_path):
    state = RuntimeState()
    state.bind_task_references("Follow missing.md", workspace=tmp_path)
    assert state.task_reference_evidence[0]["capture_status"] == "unavailable"
    (tmp_path / "missing.md").write_text("Agent created new authorization")
    state.bind_task_references("Follow missing.md", workspace=tmp_path)
    assert not state.task_reference_evidence[0]["complete"]
    assert not state.task_reference_evidence[0]["text"]


def test_real_sidecar_cache_invalidated_by_reference_identity(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    state = _bound(tmp_path)
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    function=SimpleNamespace(
                                        name="emit_sidecar_verdict",
                                        arguments='{"verdict":"accept","reason":"controlled verdict"}',
                                    )
                                )
                            ],
                        )
                    )
                ]
            )

    hook = create_sidecar_verifier_hook(
        SimpleNamespace(),
        ResolvedVerifierProvider(
            Provider(), object(), "offline", "offline", "inherit-main"
        ),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )

    def invoke():
        return asyncio.run(
            hook(
                StopHookContext(
                    transcript=({"role": "user", "content": TASK},),
                    last_assistant_text="done",
                    runtime_state=state.to_dict(),
                )
            )
        )

    invoke()
    invoke()
    assert len(requests) == 1
    state.task_reference_evidence[0]["model_observed"] = True
    invoke()
    assert len(requests) == 2
    state.task_reference_evidence[0]["complete"] = False
    invoke()
    assert len(requests) == 3
    assert "Complete: false" in requests[-1]["messages"][1]["content"]


def test_reference_capture_parent_swap_cannot_escape(monkeypatch, tmp_path):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "specs").mkdir(parents=True)
    outside.mkdir()
    (workspace / "specs/contract.md").write_text(SPEC)
    (outside / "contract.md").write_text("OUTSIDE_AUTHORITY_SENTINEL")
    original = WorkspacePathPolicy.validate_model_read
    swapped = False

    def validate_then_swap(policy, path):
        nonlocal swapped
        result = original(policy, path)
        if not swapped:
            swapped = True
            (workspace / "specs").rename(workspace / "original-specs")
            (workspace / "specs").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_read", validate_then_swap)
    state = RuntimeState()
    state.bind_task_references("Follow specs/contract.md", workspace=workspace)
    assert not state.task_reference_evidence[0]["complete"]
    assert "OUTSIDE_AUTHORITY_SENTINEL" not in json.dumps(state.task_reference_evidence)


def test_reference_capture_passes_hard_read_limit(monkeypatch, tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.verification.reference_evidence import MAX_REFERENCE_BYTES

    original = WorkspaceFileAccess.read_bytes_with_identity
    observed = []

    def bounded_read(access, path, *, maximum=None):
        observed.append(maximum)
        assert 0 <= maximum <= MAX_REFERENCE_BYTES
        return original(access, path, maximum=maximum)

    monkeypatch.setattr(WorkspaceFileAccess, "read_bytes_with_identity", bounded_read)
    state = _bound(tmp_path)
    assert observed == [MAX_REFERENCE_BYTES]
    assert state.task_reference_evidence[0]["complete"]


def test_rolling_reference_packet_is_independent_and_ordered(tmp_path):
    state = _bound(tmp_path)
    transcript = [{"role": "user", "content": TASK}, {"role": "tool", "content": SPEC}]
    transcript += [
        {"role": "assistant", "content": str(i)} for i in range(ROLLING_BUFFER_SIZE + 5)
    ]
    context = build_verifier_context(
        transcript,
        "done",
        file_edits=[],
        authoritative_references=state.task_reference_evidence,
    )
    assert len(context.recent_transcript) == ROLLING_BUFFER_SIZE == 24
    assert all(SPEC not in m["content"] for m in context.recent_transcript)
    rendered = build_verifier_user_message(context)
    assert (
        rendered.index("=== USER REQUEST")
        < rendered.index("=== AUTHORITATIVE TASK REFERENCES")
        < rendered.index("=== RECENT MAIN")
    )
    assert json.dumps(SPEC) in rendered


@pytest.mark.parametrize("checkpoint", ["none", "legacy", "retained"])
def test_lifecycle_resume_does_not_capture_current_rules(
    monkeypatch, tmp_path, checkpoint
):
    from types import SimpleNamespace
    from test_native_product_runtime import _request, _runtime
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.adapters import lifecycle
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.state.sessions import session_runtime_state_path
    from nz_coder.state.workdir import scoped_workdir

    monkeypatch.setenv("HOME", str(tmp_path.parent / (tmp_path.name + "-home")))
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    monkeypatch.setattr(
        lifecycle,
        "current_run_settings",
        lambda: SimpleNamespace(runtime_state_persist=True),
    )
    original = _bound(tmp_path)
    (tmp_path / "REQUIREMENTS.md").write_text("AGENT_REWRITTEN_RULES")
    env = native_sdk.build_product_run_environment(_request(tmp_path), RunOptions())
    try:
        with scoped_workdir(tmp_path):
            if checkpoint != "none":
                p = session_runtime_state_path(env.session_id)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps(
                        original.to_dict()
                        if checkpoint == "retained"
                        else {"active": True, "initial_task_text": TASK}
                    )
                )
            lifecycle.lifecycle_context_from_legacy_host(env).prepare_runtime_state(
                TASK, 12, 0, True, ""
            )
            refs = env.runtime_state.task_reference_evidence
            if checkpoint == "retained":
                assert refs[0]["text"] == SPEC
            else:
                assert refs == []
    finally:
        env.close()
