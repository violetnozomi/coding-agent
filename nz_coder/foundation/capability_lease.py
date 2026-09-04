"""Process-local ownership records for capabilities that may outlive one Run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import threading
import time
from typing import Callable
import uuid


@dataclass(frozen=True)
class CapabilityLease:
    """Immutable authority identity attached to one live external resource."""

    lease_id: str
    kind: str
    resource_id: str
    workspace_identity: str
    control_fingerprint: str
    run_id: str
    interaction_id: str
    created_at: float
    owner_session: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RevokeResult:
    matched: int
    revoked: int
    failed: int


class CapabilityLeaseRegistry:
    """Own callbacks and public, secret-free metadata for live capabilities."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._leases: dict[str, tuple[CapabilityLease, Callable[[], None]]] = {}

    def _ensure_process(self) -> None:
        if self._pid == os.getpid():
            return
        self._leases.clear()
        self._pid = os.getpid()

    def create(
        self,
        *,
        kind: str,
        resource_id: str,
        workspace: Path,
        control_fingerprint: str,
        run_id: str,
        interaction_id: str,
        owner_session: str,
        revoke: Callable[[], None],
    ) -> CapabilityLease:
        if not callable(revoke):
            raise TypeError("capability revoke callback must be callable")
        lease = CapabilityLease(
            lease_id=f"lease-{uuid.uuid4().hex}",
            kind=str(kind)[:80],
            resource_id=str(resource_id)[:200],
            workspace_identity=_workspace_identity(workspace),
            control_fingerprint=str(control_fingerprint)[:128],
            run_id=str(run_id)[:200],
            interaction_id=str(interaction_id)[:200],
            created_at=time.time(),
            owner_session=str(owner_session)[:200],
        )
        with self._lock:
            self._ensure_process()
            self._leases[lease.lease_id] = (lease, revoke)
        return lease

    def get(self, lease_id: str) -> CapabilityLease | None:
        with self._lock:
            self._ensure_process()
            record = self._leases.get(str(lease_id))
            return record[0] if record is not None else None

    def release(self, lease_id: str) -> bool:
        with self._lock:
            self._ensure_process()
            return self._leases.pop(str(lease_id), None) is not None

    def list_workspace(self, workspace: Path) -> list[CapabilityLease]:
        identity = _workspace_identity(workspace)
        with self._lock:
            self._ensure_process()
            leases = [
                lease for lease, _callback in self._leases.values()
                if lease.workspace_identity == identity
            ]
        return sorted(leases, key=lambda item: (item.created_at, item.lease_id))

    def revoke_workspace(self, workspace: Path) -> RevokeResult:
        identity = _workspace_identity(workspace)
        with self._lock:
            self._ensure_process()
            records = [
                (lease_id, lease, callback)
                for lease_id, (lease, callback) in self._leases.items()
                if lease.workspace_identity == identity
            ]
        revoked = 0
        failed = 0
        for lease_id, _lease, callback in records:
            try:
                callback()
            except BaseException:
                failed += 1
                continue
            with self._lock:
                self._leases.pop(lease_id, None)
            revoked += 1
        return RevokeResult(len(records), revoked, failed)


def _workspace_identity(workspace: Path) -> str:
    target = Path(workspace).resolve(strict=True)
    info = target.stat()
    return f"{os.path.normcase(str(target))}|{info.st_dev}|{info.st_ino}"


_REGISTRY = CapabilityLeaseRegistry()


def capability_leases() -> CapabilityLeaseRegistry:
    return _REGISTRY


__all__ = [
    "CapabilityLease", "CapabilityLeaseRegistry", "RevokeResult",
    "capability_leases",
]
