"""项目记忆身份独立于子任务的源码执行目录。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
import re

from nz_coder.foundation.user_paths import user_storage_layout
from nz_coder.protocol.public_error import PublicInputError


@dataclass(frozen=True)
class MemoryScope:
    scope_id: str
    owner_workspace_key: str

    def __post_init__(self):
        if (not re.fullmatch(r"[0-9a-f]{64}", self.scope_id)
                or self.owner_workspace_key != self.scope_id):
            raise PublicInputError("Invalid project memory scope")

    @classmethod
    def for_workspace(cls, workspace: Path | str):
        key = user_storage_layout(workspace).workspace_key
        return cls(key, key)

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or set(value) != {"scope_id", "owner_workspace_key"}:
            raise PublicInputError("Invalid serialized memory scope")
        return cls(**value)

    def to_dict(self):
        return asdict(self)

    def directory(self, execution_root: Path | str):
        # 只接收不透明身份；模型不能借scope传入任意数据库路径。
        return user_storage_layout(execution_root).state_root / "workspaces" / self.owner_workspace_key / "memory"


_SCOPE: ContextVar[MemoryScope | None] = ContextVar("project_memory_scope", default=None)


def current_memory_scope():
    return _SCOPE.get()


def session_memory_binding(session_id: str, workspace: Path | str):
    """只从该会话的私有持久记录恢复范围，不信请求正文中的scope。"""
    from nz_coder.state.sessions import load_session
    from nz_coder.state.workdir import scoped_workdir
    with scoped_workdir(workspace):
        metadata = load_session(session_id).get("metadata") or {}
    raw = metadata.get("memory_scope")
    persisted = MemoryScope.from_dict(raw) if raw is not None else None
    bound = current_memory_scope()
    if bound is not None and persisted is not None and bound != persisted:
        raise PublicInputError("Restored memory scope does not match the trusted parent scope")
    capability = metadata.get("memory_capability", "manage")
    if capability not in {"read", "propose", "manage"}:
        raise PublicInputError("Invalid persisted memory capability")
    return bound or persisted or MemoryScope.for_workspace(workspace), capability


@contextmanager
def scoped_memory_scope(scope: MemoryScope | None):
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)
