"""准备期快照的身份、有界内容及原briefing顺序。"""

from dataclasses import replace
import subprocess

import pytest

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.runtime.agent import child_contracts
from tests.fixtures.process_lifecycle import make_preparation_scope


def capture(workspace, refs, preparation):
    from nz_coder.runtime.agent.prepared_evidence import capture_diff_evidence

    return capture_diff_evidence(
        refs,
        workspace=workspace,
        parent_session_id="parent",
        child_session_id="child",
        settlement_id="round",
        preparation=preparation,
    )


def bind(workspace, snapshot, refs):
    from nz_coder.runtime.agent.prepared_evidence import bind_diff_lookup

    return bind_diff_lookup(
        snapshot,
        workspace=workspace,
        parent_session_id="parent",
        child_session_id="child",
        settlement_id="round",
        refs=refs,
    )


@pytest.mark.parametrize(
    "case",
    ["text", "empty", "nonzero", "path", "os", "timeout", "decode", "cancel", "infra"],
)
def test_capture_diff_uses_parent_scope_and_preserves_limits(
    tmp_path, monkeypatch, case
):
    from nz_coder.runtime.process.preparation import (
        PreparationInterrupted,
        PreparationFailure,
    )

    scope = make_preparation_scope(tmp_path)
    calls = []

    def git(args, *, cwd, timeout):
        calls.append((args, cwd, timeout))
        if case == "os":
            raise OSError("private token")
        if case == "timeout":
            raise subprocess.TimeoutExpired("private token", 5)
        if case == "decode":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private token")
        if case == "cancel":
            raise PreparationInterrupted("cancelled")
        if case == "infra":
            raise PreparationFailure(
                PublicError("preparation_io_error", "Preparation failed.")
            )
        return subprocess.CompletedProcess(
            args,
            1 if case == "nonzero" else 0,
            "" if case == "empty" else "x" * 7000,
            "private token",
        )

    monkeypatch.setattr(scope, "run_git", git)
    ref = "diff:../outside" if case == "path" else "diff:app.py"
    try:
        if case in {"cancel", "infra"}:
            with pytest.raises(
                PreparationInterrupted if case == "cancel" else PreparationFailure
            ):
                capture(tmp_path, [ref], scope)
        else:
            snapshot = capture(tmp_path, [ref], scope)
            entry = snapshot.entries[0]
            if case in {"text", "empty"}:
                assert entry.text == (
                    "x" * 6000 if case == "text" else "(no current diff for this path)"
                )
                assert bind(tmp_path, snapshot, [ref])(ref) == entry.text
            else:
                assert entry.text is None and entry.error is not None
                assert len(entry.error.message) <= 500 and "private token" not in str(
                    entry.error
                )
                with pytest.raises(ValueError):
                    bind(tmp_path, snapshot, [ref])(ref)
        assert calls == (
            []
            if case == "path"
            else [(("diff", "--no-ext-diff", "--", "app.py"), tmp_path, 5)]
        )
    finally:
        assert scope.close(deadline=0)


@pytest.mark.parametrize(
    "case",
    [
        "root",
        "identity",
        "parent",
        "child",
        "settlement",
        "refs",
        "missing",
        "extra",
        "both",
        "neither",
        "text_limit",
        "error_limit",
        "refs_limit",
        "ref_limit",
        "projection_limit",
        "wrong_type",
    ],
)
def test_diff_snapshot_identity_and_shape_fail_closed(tmp_path, monkeypatch, case):
    from nz_coder.runtime.agent.prepared_evidence import DiffEvidenceEntry

    scope = make_preparation_scope(tmp_path)
    monkeypatch.setattr(
        scope,
        "run_git",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "ok", ""),
    )
    refs = ["diff:app.py"]
    snapshot = capture(tmp_path, refs, scope)
    entry = snapshot.entries[0]
    changes = {
        "root": {"parent_workspace": tmp_path.parent},
        "identity": {"parent_identity": (0, 0)},
        "parent": {"parent_session_id": "other"},
        "child": {"child_session_id": "other"},
        "settlement": {"settlement_id": "other"},
        "refs": {"refs": ("finding:other",)},
        "missing": {"entries": ()},
        "extra": {"entries": (entry, entry)},
        "both": {
            "entries": (replace(entry, error=PublicError("invalid_input", "bad")),)
        },
        "neither": {"entries": (replace(entry, text=None),)},
        "text_limit": {"entries": (replace(entry, text="x" * 6001),)},
        "error_limit": {
            "entries": (
                DiffEvidenceEntry(
                    refs[0], None, PublicError("invalid_input", "x" * 501)
                ),
            )
        },
        "refs_limit": {"refs": tuple(f"finding:{i}" for i in range(21))},
        "ref_limit": {"refs": ("finding:" + "x" * 2000,)},
        "projection_limit": {"parent_session_id": "x" * (2 * 1024 * 1024)},
        "wrong_type": {"entries": (None,)},
    }
    snapshot = replace(snapshot, **changes[case])
    monkeypatch.setattr(
        child_contracts.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("快照无效不得转子方Git"),
    )
    try:
        with pytest.raises(PublicRuntimeError) as exc:
            bind(tmp_path, snapshot, refs)
        assert exc.value.public_error.code == "prepared_evidence_invalid"
    finally:
        assert scope.close(deadline=0)


def test_maximum_legal_control_text_and_empty_snapshot(tmp_path, monkeypatch):
    scope = make_preparation_scope(tmp_path)
    monkeypatch.setattr(
        scope,
        "run_git",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "\x00" * 6000, ""),
    )
    refs = ["diff:" + str(i) + "x" * 1988 for i in range(20)]
    try:
        snapshot = capture(tmp_path, refs, scope)
        lookup = bind(tmp_path, snapshot, refs)
        assert all(lookup(ref) == "\x00" * 6000 for ref in refs)
        empty = capture(tmp_path, [], scope)
        bind(tmp_path, empty, [])
        with pytest.raises(PublicRuntimeError):
            bind(tmp_path, replace(empty, settlement_id="other"), [])
    finally:
        assert scope.close(deadline=0)


@pytest.mark.parametrize("error_at", [None, 3, 4])
def test_prepared_diff_preserves_mixed_briefing_and_zero_budget_order(
    tmp_path, monkeypatch, error_at
):
    calls = []
    refs = [f"finding:{i}" for i in range(5)]

    def resolve(ref, **kwargs):
        calls.append(ref)
        if error_at is not None and ref == refs[error_at]:
            raise ValueError("consumed error")
        return "x" * 6000

    with monkeypatch.context() as patch:
        patch.setattr(child_contracts, "_resolve_evidence_ref", resolve)
        if error_at == 3:
            with pytest.raises(ValueError, match="consumed error"):
                child_contracts.build_evidence_briefing(
                    refs, workspace=tmp_path, load_task_state=lambda _: {}
                )
        else:
            result = child_contracts.build_evidence_briefing(
                refs, workspace=tmp_path, load_task_state=lambda _: {}
            )
            assert "untrusted context" in result and len(result) < 16500
    assert calls == refs[:4]
    calls.clear()
    diffs = [f"diff:{i}" for i in range(5)]

    def lookup(ref):
        return resolve(ref.replace("diff:", "finding:"))

    if error_at == 3:
        with pytest.raises(ValueError, match="consumed error"):
            child_contracts.build_evidence_briefing(
                diffs,
                workspace=tmp_path,
                load_task_state=lambda _: {},
                diff_lookup=lookup,
            )
    else:
        child_contracts.build_evidence_briefing(
            diffs, workspace=tmp_path, load_task_state=lambda _: {}, diff_lookup=lookup
        )
    assert calls == refs[:4]
    (tmp_path / "a.py").write_text("FILE")
    result = child_contracts.build_evidence_briefing(
        ["file:a.py", "finding:CHECK", "task_id:old", "diff:a.py"],
        workspace=tmp_path,
        load_task_state=lambda _: {"background_result": "TASK"},
        diff_lookup=lambda _: "DIFF",
    )
    assert (
        result.index("FILE")
        < result.index("CHECK")
        < result.index("TASK")
        < result.index("DIFF")
    )


@pytest.mark.parametrize(
    "declared,state,expected",
    [
        (None, ["finding:a"], ["finding:a"]),
        (["finding:a", "finding:a"], [], ["finding:a"]),
        (None, [], []),
        ([], [], []),
    ],
)
def test_effective_refs_preserve_resume_rules(declared, state, expected):
    assert child_contracts.resolve_effective_evidence_refs(declared, state) == expected
    for invalid in [["other:a"], ["diff:"], []]:
        with pytest.raises(ValueError):
            child_contracts.resolve_effective_evidence_refs(invalid, ["finding:a"])


@pytest.mark.parametrize(
    "field", ["parent_session_id", "child_session_id", "settlement_id"]
)
def test_snapshot_identity_fields_must_be_strings_even_when_expected_is_invalid(
    tmp_path, monkeypatch, field
):
    scope = make_preparation_scope(tmp_path)
    try:
        snapshot = capture(tmp_path, [], scope)
        snapshot = replace(snapshot, **{field: None})
        from nz_coder.runtime.agent.prepared_evidence import bind_diff_lookup

        kwargs = dict(
            workspace=tmp_path,
            parent_session_id="parent",
            child_session_id="child",
            settlement_id="round",
            refs=[],
        )
        kwargs[field] = None
        with pytest.raises(PublicRuntimeError):
            bind_diff_lookup(snapshot, **kwargs)
    finally:
        assert scope.close(deadline=0)


def test_snapshot_projection_limit_is_checked_after_matching_identity(tmp_path):
    from nz_coder.runtime.agent.prepared_evidence import bind_diff_lookup

    scope = make_preparation_scope(tmp_path)
    try:
        huge = "x" * (2 * 1024 * 1024)
        snapshot = replace(capture(tmp_path, [], scope), parent_session_id=huge)
        with pytest.raises(PublicRuntimeError):
            bind_diff_lookup(
                snapshot,
                workspace=tmp_path,
                parent_session_id=huge,
                child_session_id="child",
                settlement_id="round",
                refs=[],
            )
    finally:
        assert scope.close(deadline=0)


def test_effective_refs_immutability_keeps_original_public_message():
    from nz_coder.protocol.public_error import format_public_error

    with pytest.raises(ValueError) as error:
        child_contracts.resolve_effective_evidence_refs([], ["finding:original"])
    assert (
        format_public_error(error.value)
        == "Error: evidence_refs cannot change when resuming a child session"
    )
