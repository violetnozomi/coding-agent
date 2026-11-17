"""本次process启动的父工作区diff快照；不替代其它证据服务。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.runtime.agent.child_contracts import (
    normalize_evidence_refs,
    _safe_evidence_path,
)
from nz_coder.runtime.process.command_broker import command_directory_identity
from nz_coder.runtime.process.preparation import (
    PreparationScope,
    PreparationInterrupted,
    PreparationFailure,
)


@dataclass(frozen=True)
class DiffEvidenceEntry:
    ref: str
    text: str | None
    error: PublicError | None


@dataclass(frozen=True)
class PreparedDiffEvidence:
    parent_workspace: Path
    parent_identity: tuple[int, int]
    parent_session_id: str
    child_session_id: str
    settlement_id: str
    refs: tuple[str, ...]
    entries: tuple[DiffEvidenceEntry, ...]


def _invalid() -> PublicRuntimeError:
    return PublicRuntimeError(
        PublicError(
            "prepared_evidence_invalid",
            "Prepared evidence does not match this invocation.",
        )
    )


def bind_diff_lookup(
    snapshot: PreparedDiffEvidence,
    *,
    workspace: Path,
    parent_session_id: str,
    child_session_id: str,
    settlement_id: str,
    refs: list[str],
) -> Callable[[str], str]:
    try:
        root = Path(workspace).resolve()
        expected_refs = tuple(normalize_evidence_refs(refs))
        if not isinstance(snapshot, PreparedDiffEvidence):
            raise ValueError("missing snapshot")
        if any(
            type(value) is not str
            for value in (
                snapshot.parent_session_id,
                snapshot.child_session_id,
                snapshot.settlement_id,
                parent_session_id,
                child_session_id,
                settlement_id,
            )
        ):
            raise ValueError("invalid invocation identity types")
        if (
            snapshot.parent_workspace != root
            or snapshot.parent_identity != command_directory_identity(root)
            or snapshot.parent_session_id != parent_session_id
            or snapshot.child_session_id != child_session_id
            or snapshot.settlement_id != settlement_id
            or type(snapshot.refs) is not tuple
            or snapshot.refs != expected_refs
            or type(snapshot.entries) is not tuple
        ):
            raise ValueError("identity mismatch")
        expected_diffs = tuple(ref for ref in expected_refs if ref.startswith("diff:"))
        if len(snapshot.entries) != len(expected_diffs):
            raise ValueError("entry count mismatch")
        projection = []
        for ref, entry in zip(expected_diffs, snapshot.entries):
            if not isinstance(entry, DiffEvidenceEntry) or entry.ref != ref:
                raise ValueError("entry mismatch")
            if (entry.text is None) == (entry.error is None):
                raise ValueError("entry must have exactly one result")
            if entry.text is not None and (
                not isinstance(entry.text, str) or len(entry.text) > 6000
            ):
                raise ValueError("text limit")
            error = entry.error
            if error is not None:
                if (
                    not isinstance(error, PublicError)
                    or not isinstance(error.code, str)
                    or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", error.code) is None
                    or not isinstance(error.message, str)
                    or not error.message
                    or len(error.message) > 500
                    or type(error.retryable) is not bool
                    or error.metadata
                ):
                    raise ValueError("invalid safe error")
            projection.append(
                {
                    "ref": ref,
                    "text": entry.text,
                    "error": error.to_dict() if error else None,
                }
            )
        document = {
            "workspace": str(root),
            "identity": snapshot.parent_identity,
            "parent": snapshot.parent_session_id,
            "child": snapshot.child_session_id,
            "settlement": snapshot.settlement_id,
            "refs": snapshot.refs,
            "entries": projection,
        }
        if (
            len(
                json.dumps(document, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            )
            > 2 * 1024 * 1024
        ):
            raise ValueError("snapshot byte limit")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise _invalid() from exc
    # 只引用已验证的冻结条目，不在消费期重新执行Git或读取其它证据。
    entries = {entry.ref: entry for entry in snapshot.entries}

    def lookup(ref: str) -> str:
        if ref not in entries:
            raise _invalid()
        entry = entries[ref]
        if entry.error is not None:
            raise ValueError(entry.error.message)
        return entry.text

    return lookup


def capture_diff_evidence(
    refs: list[str],
    *,
    workspace: Path,
    parent_session_id: str,
    child_session_id: str,
    settlement_id: str,
    preparation: PreparationScope,
) -> PreparedDiffEvidence:
    preparation.checkpoint()
    root = Path(workspace).resolve()
    identity = command_directory_identity(root)
    normalized = normalize_evidence_refs(refs)
    entries = []
    for ref in normalized:
        if not ref.startswith("diff:"):
            continue
        preparation.checkpoint()
        try:
            value = ref.split(":", 1)[1].strip()
            _safe_evidence_path(root, value)
            result = preparation.run_git(
                ("diff", "--no-ext-diff", "--", value), cwd=root, timeout=5
            )
            if result.returncode != 0:
                raise ValueError("diff command failed")
            entries.append(
                DiffEvidenceEntry(
                    ref,
                    (result.stdout or "(no current diff for this path)")[:6000],
                    None,
                )
            )
        except (PreparationInterrupted, PreparationFailure):
            raise
        except (ValueError, OSError, subprocess.SubprocessError, UnicodeError):
            # 普通项错误延迟到原briefing真正消费时；不保存stderr和异常链。
            entries.append(
                DiffEvidenceEntry(
                    ref,
                    None,
                    PublicError("invalid_input", "Cannot resolve diff evidence."),
                )
            )
    preparation.checkpoint()
    snapshot = PreparedDiffEvidence(
        root,
        identity,
        parent_session_id,
        child_session_id,
        settlement_id,
        tuple(normalized),
        tuple(entries),
    )
    bind_diff_lookup(
        snapshot,
        workspace=root,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        settlement_id=settlement_id,
        refs=normalized,
    )
    return snapshot
