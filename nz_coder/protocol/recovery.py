"""Versioned recovery results shared by storage, runtime and product adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from nz_coder.foundation.content_objects import SnapshotError


@dataclass(frozen=True)
class SessionRevertResult:
    """Additive shared result for CLI, SDK and HTTP recovery commands."""

    message_id: str
    files: tuple[str, ...]
    removed_messages: int
    status: str = "completed"
    operation_id: str = ""
    conflicts: tuple[str, ...] = ()
    recovery_required: bool = False
    unsupported_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"version": 1, **asdict(self)}


class RecoveryError(SnapshotError):
    """An inspectable refusal with retained operation and conflict identities."""

    def __init__(self, message: str, *, operation_id="", conflicts=(), recovery_required=True):
        super().__init__(message)
        self.result = SessionRevertResult("", (), 0, "conflicted" if conflicts else "recovery_required",
                                         operation_id, tuple(conflicts), recovery_required)
