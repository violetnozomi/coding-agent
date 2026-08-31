"""Workspace snapshot and per-step patch observation boundary."""
from __future__ import annotations


class LegacySnapshotRuntime:
    """Preserve coding snapshot behavior behind one focused service owner."""

    def __init__(self, host) -> None:
        self._host = host

    def capture(self, *args, **kwargs):
        return self._required("_capture_step_snapshot")(*args, **kwargs)

    async def await_start(self, *args, **kwargs):
        return await self._required("_await_step_start_snapshot")(*args, **kwargs)

    def retire(self, *args, **kwargs):
        return self._required("_retire_snapshot_task")(*args, **kwargs)

    async def capture_async(self, *args, **kwargs):
        return await self._required("_capture_step_snapshot_async")(*args, **kwargs)

    def record_patch(self, *args, **kwargs):
        return self._required("_record_step_patch")(*args, **kwargs)

    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"SnapshotRuntime is missing required capability {name}")
        return value
