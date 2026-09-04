"""Immutable ownership record for one top-level product run control epoch."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any


class RunControlCleanupError(RuntimeError):
    """Secret-free signal that one or more owned resources remain live."""

    def __init__(self, resources: tuple[str, ...]):
        self.resources = resources
        super().__init__(
            "Run-control resource cleanup failed: " + ", ".join(resources)
        )


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
    _sidecar_closed: bool = field(default=False, init=False, repr=False)
    _mcp_closed: bool = field(default=False, init=False, repr=False)
    _provider_runtime_ids_closed: set[int] = field(
        default_factory=set, init=False, repr=False,
    )
    _cleanup_failures: dict[str, str] = field(
        default_factory=dict, init=False, repr=False,
    )
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )

    def close(self) -> None:
        """Retire owned resources, retrying only stages not yet completed."""
        with self._close_lock:
            if self._closed:
                return
            failures: list[tuple[str, BaseException]] = []
            close_sidecar = (
                getattr(self.sidecar_verifier, "close", None)
                if self.owns_sidecar_verifier
                else None
            )
            if not self._sidecar_closed:
                if callable(close_sidecar):
                    try:
                        close_sidecar()
                    except BaseException as exc:  # cleanup must continue
                        failures.append(("sidecar", exc))
                    else:
                        self._sidecar_closed = True
                        self._cleanup_failures.pop("sidecar", None)
                else:
                    self._sidecar_closed = True
            close_mcp = getattr(self.mcp_runtime, "close", None)
            if not self._mcp_closed:
                if callable(close_mcp):
                    try:
                        close_mcp()
                    except BaseException as exc:  # cleanup must continue
                        failures.append(("mcp", exc))
                    else:
                        self._mcp_closed = True
                        self._cleanup_failures.pop("mcp", None)
                else:
                    self._mcp_closed = True
            unique = (
                {id(value): value for value in self.provider_runtimes.values()}
                if self.owns_provider_runtimes else {}
            )
            for runtime_id, runtime in unique.items():
                if runtime_id in self._provider_runtime_ids_closed:
                    continue
                label = f"provider:{runtime_id}"
                try:
                    runtime.close()
                except BaseException as exc:  # cleanup must continue
                    failures.append((label, exc))
                else:
                    self._provider_runtime_ids_closed.add(runtime_id)
                    self._cleanup_failures.pop(label, None)
            for label, exc in failures:
                self._cleanup_failures[label] = type(exc).__name__
            self._closed = bool(
                self._sidecar_closed
                and self._mcp_closed
                and len(self._provider_runtime_ids_closed) == len(unique)
            )
            if failures:
                raise RunControlCleanupError(
                    tuple(label for label, _exc in failures)
                ) from failures[0][1]

    @property
    def completed(self) -> bool:
        with self._close_lock:
            return self._closed

    @property
    def cleanup_failures(self) -> dict[str, str]:
        with self._close_lock:
            return dict(self._cleanup_failures)

    @property
    def incomplete_resources(self) -> tuple[str, ...]:
        with self._close_lock:
            pending: list[str] = []
            if not self._sidecar_closed:
                pending.append("sidecar")
            if not self._mcp_closed:
                pending.append("mcp")
            if self.owns_provider_runtimes:
                for runtime_id in sorted({
                    id(value) for value in self.provider_runtimes.values()
                }):
                    if runtime_id not in self._provider_runtime_ids_closed:
                        pending.append(f"provider:{runtime_id}")
            return tuple(pending)


__all__ = ["RunControlBundle", "RunControlCleanupError"]
