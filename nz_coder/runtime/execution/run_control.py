"""Immutable ownership record for one top-level product run control epoch."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any


@dataclass
class RunControlBundle:
    """All security-sensitive controls selected from one ConfigSnapshot."""

    config_snapshot: Any
    permissions: Any
    plan_mode: Any
    skill_loader: Any
    hooks: Any
    mcp_runtime: Any
    model_runtime: Any
    provider_runtimes: dict[tuple[str, str], Any]
    owns_provider_runtimes: bool
    sidecar_verifier: Any = None
    owns_sidecar_verifier: bool = False
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )

    def close(self) -> None:
        """Retire every resource owned by this epoch exactly once."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        failures: list[BaseException] = []
        close_sidecar = (
            getattr(self.sidecar_verifier, "close", None)
            if self.owns_sidecar_verifier
            else None
        )
        if callable(close_sidecar):
            try:
                close_sidecar()
            except BaseException as exc:  # cleanup must continue
                failures.append(exc)
        close_mcp = getattr(self.mcp_runtime, "close", None)
        if callable(close_mcp):
            try:
                close_mcp()
            except BaseException as exc:  # cleanup must continue
                failures.append(exc)
        if self.owns_provider_runtimes:
            unique = {id(value): value for value in self.provider_runtimes.values()}
            for runtime in unique.values():
                try:
                    runtime.close()
                except BaseException as exc:  # cleanup must continue
                    failures.append(exc)
        if failures:
            raise RuntimeError("Run-control resource cleanup failed") from failures[0]


__all__ = ["RunControlBundle"]
