"""将终端导出的 Markdown 原子写入用户选择的本地路径。"""
from __future__ import annotations

from pathlib import Path

from nz_coder.foundation.workspace_file_access import FixedFileAccess


def write_transcript(path: str, payload: dict[str, str], *, workspace: Path | None = None) -> Path:
    raw = Path(path).expanduser()
    candidate = workspace / raw if workspace is not None else raw.absolute()
    if candidate.is_symlink():
        raise ValueError("Export target symlinks are not allowed")
    target = candidate.resolve()
    root = workspace.resolve() if workspace is not None else target.parent
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Export path must stay inside the workspace") from exc
    if target.exists() and not target.is_file():
        raise ValueError("Export target must be a regular file")
    notice = payload.get("notice")
    if notice and target.exists():
        raise ValueError(f"{notice} Choose a new path to preserve the existing file.")
    # 不完整的还原也可另存。即使有并发导出，也不能覆盖已存在的完整原件。
    access = FixedFileAccess(root, (relative,))
    access.write_bytes(relative, payload["markdown"].encode("utf-8"), mode=0o600, overwrite=not notice)
    return target
