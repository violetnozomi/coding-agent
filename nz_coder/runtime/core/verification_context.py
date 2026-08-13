"""Focused operations required for completion verification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class VerificationExecutionContext:
    """Completion-review callbacks without retaining an Agent host."""

    override: Callable[..., object] | None
    review: Callable[..., object]

    def __post_init__(self) -> None:
        if self.override is not None and not callable(self.override):
            raise TypeError("Verification override must be callable or None")
        if not callable(self.review):
            raise TypeError("Verification review must be callable")
