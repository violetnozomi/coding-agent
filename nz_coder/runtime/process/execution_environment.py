"""当前激活的不可变命令环境；不修改进程全局环境。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError


@dataclass(frozen=True)
class ExecutionEnvironment:
    variables: tuple[tuple[str, str], ...] = field(repr=False)
    fingerprint: str
    inputs: tuple[tuple[str, str], ...] = ()
    resources: tuple[tuple[str, int, int], ...] = ()
    excluded: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    origin_workspaces: tuple[tuple[str, int, int], ...] = ()

    def __post_init__(self) -> None:
        # replace 补入私有资源后也必须重新绑定身份，不能沿用只有变量的指纹。
        payload = (self.variables, self.inputs, self.resources, self.excluded,
                   self.source_paths, self.origin_workspaces)
        object.__setattr__(self, "fingerprint", hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest())

    @classmethod
    def capture(
        cls,
        overrides=None,
        *,
        source=None,
        inputs=(),
        resources=(),
        excluded=(),
        source_paths=(),
        origin_workspaces=(),
    ):
        values = build_sanitized_subprocess_env(source=source, overrides=overrides)
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        return cls(
            tuple(sorted(values.items())),
            digest,
            tuple(inputs),
            tuple(resources),
            tuple(excluded),
            tuple(source_paths),
            tuple(origin_workspaces),
        )

    def validate(self) -> None:
        for path, device, inode in self.origin_workspaces:
            try:
                root = Path(path)
                info = root.stat()
                valid = root.is_dir() and str(root.resolve()) == path and (info.st_dev, info.st_ino) == (device, inode)
            except OSError:
                valid = False
            if not valid:
                raise PublicRuntimeError(PublicError(
                    "environment_origin_changed", "Prepared environment origin workspace changed; reprepare.",
                ))
        for path, device, inode in self.resources:
            try:
                info = Path(path).stat()
            except OSError as exc:
                raise PublicRuntimeError(
                    PublicError(
                        "environment_resource_changed",
                        "Prepared environment resource is unavailable; reprepare.",
                    )
                ) from exc
            if (info.st_dev, info.st_ino) != (device, inode):
                raise PublicRuntimeError(
                    PublicError(
                        "environment_resource_changed",
                        "Prepared environment resource changed; reprepare.",
                    )
                )
        for path, expected in self.inputs:
            file = Path(path)
            current = (
                hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else ""
            )
            if current != expected:
                raise PublicRuntimeError(
                    PublicError(
                        "environment_inputs_changed",
                        "Environment inputs changed; restore inputs or reprepare in a settled activation.",
                    )
                )

    def as_dict(self) -> dict[str, str]:
        self.validate()
        return dict(self.variables)


_CURRENT: ContextVar[ExecutionEnvironment | None] = ContextVar(
    "execution_environment", default=None
)


def current_execution_environment() -> ExecutionEnvironment | None:
    return _CURRENT.get()


@contextmanager
def scoped_execution_environment(environment):
    token = _CURRENT.set(environment)
    try:
        yield
    finally:
        _CURRENT.reset(token)
