"""Context-local workspace selection for agent execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from nz_coder import config

_DERIVED_DIRS = (
    "TRANSCRIPT_DIR",
    "TOOL_RESULTS_DIR",
    "MEMORY_DIR",
    "TRACE_DIR",
    "CHANGE_DIR",
    "SESSION_DIR",
)

_WORKDIR_OVERRIDE: ContextVar[Path | None] = ContextVar(
    "nz_coder_workdir_override",
    default=None,
)


def _workspace_derived_paths(root: Path) -> dict[str, Path]:
    base = root.resolve() / ".nz-coder"
    return {
        "TRANSCRIPT_DIR": base / "transcripts",
        "TOOL_RESULTS_DIR": base / "tool-results",
        "MEMORY_DIR": base / "memory",
        "TRACE_DIR": base / "runs",
        "CHANGE_DIR": base / "changes",
        "SESSION_DIR": base / "sessions",
    }


def current_workdir() -> Path:
    """Return the workspace bound to this execution context."""
    override = _WORKDIR_OVERRIDE.get()
    if override is not None:
        return override
    return Path(config.WORKDIR).resolve()


def current_derived_path(name: str) -> Path:
    """Return a workspace-derived directory without mutating global config."""
    if name not in _DERIVED_DIRS:
        raise ValueError(f"Unknown workspace-derived path: {name}")
    override = _WORKDIR_OVERRIDE.get()
    if override is not None:
        return _workspace_derived_paths(override)[name]
    root = current_workdir()
    expected = _workspace_derived_paths(root)[name]
    configured = Path(getattr(config, name))
    if configured.parent.name == ".nz-coder" and configured.parent.parent.resolve() != root:
        return expected
    return configured


@contextmanager
def scoped_workdir(workdir: str | Path):
    """Bind a workspace to the current thread or async task."""
    target = Path(workdir).resolve()
    token = _WORKDIR_OVERRIDE.set(target)
    try:
        yield target
    finally:
        _WORKDIR_OVERRIDE.reset(token)
