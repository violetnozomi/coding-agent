"""Shared construction of validated user submissions for terminal hosts."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterable

from nz_coder.protocol.message_schema import bind_user_context
from nz_coder.state.context import _is_compaction_summary
from nz_coder.state.input_expansion import USER_TEXT_KEY, tag_file_attachments
from nz_coder.foundation.user_paths import resolve_private_attachment


@dataclass(frozen=True)
class SubmissionFile:
    """One workspace-confined file accepted by an input surface."""

    path: str
    size: int
    host_path: str = ""


def resolve_submission_file(raw: str, workspace: Path) -> SubmissionFile:
    """Validate one attachment against the resolved workspace root."""
    if raw.startswith("user-state://attachments/"):
        try:
            actual = resolve_private_attachment(workspace, raw)
            metadata = actual.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError
            return SubmissionFile(raw, metadata.st_size, str(actual))
        except (OSError, ValueError) as exc:
            raise ValueError("Private attachment is missing or unsafe") from exc

    target = workspace / Path(raw.strip())
    try:
        if target.is_symlink():
            problem = "Attachment symlinks are not allowed"
        else:
            # realpath 保留循环软链的系统错误，隐式探测与显式附件共用同一个错误边界。
            actual = Path(os.path.realpath(target, strict=True))
            relative = actual.relative_to(workspace)
            metadata = actual.lstat()
            if stat.S_ISREG(metadata.st_mode):
                return SubmissionFile(relative.as_posix(), metadata.st_size)
            problem = "Attachment must be a regular file"
    except (OSError, ValueError) as exc:
        raise ValueError("Attachment must be a file inside the workspace") from exc
    raise ValueError(problem)


def resolve_submission_files(
    paths: Iterable[str], workspace: str | Path,
) -> list[SubmissionFile]:
    """Resolve files without allowing missing, directory, symlink, or escape paths."""
    root = Path(workspace).resolve(strict=True)
    resolved: list[SubmissionFile] = []
    for raw in paths:
        try:
            resolved.append(resolve_submission_file(str(raw), root))
        except ValueError as exc:
            raise ValueError(f"{exc}: {raw}") from exc
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
    if _is_compaction_summary(message):
        message[USER_TEXT_KEY] = message["content"]
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


__all__ = ["SubmissionFile", "build_user_submission", "resolve_submission_file", "resolve_submission_files"]
