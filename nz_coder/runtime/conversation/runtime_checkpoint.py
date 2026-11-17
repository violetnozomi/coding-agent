"""纯运行状态快照构造和原子写入，供执行与规划共同使用。"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import time


def build_runtime_checkpoint(state, *, active: bool = True) -> dict:
    """在同一运行锁内复制版本和验证域；调用方在锁外写盘。"""
    from contextlib import nullcontext
    context = state._verification_context
    with context._state_lock if context is not None else nullcontext():
        data = asdict(state)
        data.update(active=active, saved_at=time.time())
        domain = state.verification_domain
        if domain is not None:
            data["verification_domain"] = domain.checkpoint()
        return data


def write_runtime_snapshot(path: Path, snapshot: dict) -> None:
    rendered = json.dumps(
        snapshot,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
