"""Context-local workspace selection for agent execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from nz_coder.foundation import config

_DERIVED_DIRS = (
    "TRANSCRIPT_DIR",
    "TOOL_RESULTS_DIR",
    "MEMORY_DIR",
    "TRACE_DIR",
    "CHANGE_DIR",
    "SESSION_DIR",
    "ARTIFACT_DIR",
    "ATTACHMENT_DIR",
    "DOCUMENT_CACHE_DIR",
    "INDEX_CACHE_DIR",
    "MODEL_CACHE_DIR",
)

_COMPAT_DEFAULTS = {
    name: Path(getattr(config, name))
    for name in _DERIVED_DIRS
    if hasattr(config, name)
}

_WORKDIR_OVERRIDE: ContextVar[Path | None] = ContextVar(
    "nz_coder_workdir_override",
    default=None,
)


def _workspace_derived_paths(root: Path) -> dict[str, Path]:
    from nz_coder.foundation.user_paths import prepare_user_storage

    layout = prepare_user_storage(root)
    state = layout.workspace_state
    cache = layout.workspace_cache
    return {
        "TRANSCRIPT_DIR": state / "transcripts",
        "TOOL_RESULTS_DIR": state / "tool-results",
        "MEMORY_DIR": state / "memory",
        "TRACE_DIR": state / "runs",
        "CHANGE_DIR": state / "changes",
        "SESSION_DIR": state / "sessions",
        "ARTIFACT_DIR": state / "artifacts",
        "ATTACHMENT_DIR": state / "attachments",
        "DOCUMENT_CACHE_DIR": cache / "documents",
        "INDEX_CACHE_DIR": cache / "indexes",
        "MODEL_CACHE_DIR": cache / "models",
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
    configured_default = _COMPAT_DEFAULTS.get(name)
    if configured_default is not None:
        configured = Path(getattr(config, name))
        if configured != configured_default:
            return configured
    return _workspace_derived_paths(current_workdir())[name]


@contextmanager
def scoped_workdir(workdir: str | Path):
    """Bind a workspace to the current thread or async task."""
    target = Path(workdir).resolve()
    token = _WORKDIR_OVERRIDE.set(target)
    try:
        yield target
    finally:
        _WORKDIR_OVERRIDE.reset(token)
