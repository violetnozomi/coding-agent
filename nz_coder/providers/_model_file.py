"""模型状态文件的私有权限和原子写入。"""
from __future__ import annotations

from contextlib import ExitStack, suppress
import os
from pathlib import Path
import tempfile

from nz_coder.foundation.private_paths import harden_private_path


def write_private_model_file(target: Path, payload: bytes) -> None:
    """在同目录准备完整的私有文件，关闭句柄后一次替换目标。"""
    with ExitStack() as rollback:
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
        )
        temporary = Path(handle.name)

        def discard_temporary() -> None:
            # Windows 要求先关闭再删除；清理错误不能覆盖写入或替换的原始错误。
            with suppress(OSError):
                handle.close()
            with suppress(OSError):
                temporary.unlink()

        rollback.callback(discard_temporary)
        security = harden_private_path(temporary)
        if not security.hardened:
            raise PermissionError(f"Could not make model file owner-private: {security.detail}")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, target)
        rollback.pop_all()
