"""Shared construction of validated user submissions for terminal hosts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from nz_coder.message_schema import bind_user_context
from nz_coder.state.input_expansion import tag_file_attachments


@dataclass(frozen=True)
class SubmissionFile:
    """One workspace-confined file accepted by an input surface."""

    path: str
    size: int


def resolve_submission_files(
    paths: Iterable[str], workspace: str | Path,
) -> list[SubmissionFile]:
    """Resolve files without allowing missing, directory, symlink, or escape paths."""
    root = Path(workspace).resolve(strict=True)
    resolved: list[SubmissionFile] = []
    for raw in paths:
        candidate = Path(str(raw).strip())
        target = candidate if candidate.is_absolute() else root / candidate
        if target.is_symlink():
            raise ValueError(f"Attachment must not be a symlink: {raw}")
        try:
            actual = target.resolve(strict=True)
            relative = actual.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Attachment is outside the workspace or missing: {raw}") from exc
        if not actual.is_file():
            raise ValueError(f"Attachment is not a file: {raw}")
        resolved.append(SubmissionFile(relative.as_posix(), actual.stat().st_size))
    return resolved


def build_user_submission(
    text: str,
    attachments: Iterable[SubmissionFile],
    *,
    workspace: str | Path,
    session_id: str,
    agent: str,
    provider_id: str,
    model_id: str,
    variant: str | None = None,
    natural_text: str | None = None,
) -> dict:
    """Create the canonical user message and attach shared FilePart metadata."""
    message = {"role": "user", "content": str(text)}
    bind_user_context(
        message,
        agent=agent,
        provider_id=provider_id,
        model_id=model_id,
        variant=variant,
    )
    tag_file_attachments(
        message,
        str(text if natural_text is None else natural_text),
        list(attachments),
        workspace=workspace,
        session_id=session_id,
    )
    return message


__all__ = ["SubmissionFile", "build_user_submission", "resolve_submission_files"]
