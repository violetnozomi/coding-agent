"""Typed verification evidence shared by completion and evaluation."""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from enum import Enum


class VerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED_REPAIRABLE = "failed_repairable"
    BLOCKED_ENVIRONMENT = "blocked_environment"
    DEGRADED = "degraded"


_ENVIRONMENT_FAILURES = (
    re.compile(r"(?:command not found|is not recognized as an internal or external command)", re.I),
    re.compile(r"no module named ['\"]?(?:pytest|tox|nox|ruff|mypy|pyright)['\"]?", re.I),
    re.compile(r"(?:error importing|could not import|failed to load) (?:pytest )?plugin", re.I),
    re.compile(r"(?:missing|required) dependenc(?:y|ies)", re.I),
    re.compile(r"could not find (?:a )?(?:version|package|module)", re.I),
)


def is_environment_verification_failure(command: str, output: str) -> bool:
    """Separate unavailable verification infrastructure from code failures."""
    text = f"{command}\n{output}"
    if re.search(r"command exited with code 12[67]\b", output, re.I):
        return True
    return any(pattern.search(text) for pattern in _ENVIRONMENT_FAILURES)


@dataclass(frozen=True)
class VerificationEvidence:
    kind: str
    command: str
    scope: str
    status: str
    passed: bool | None
    output_reference: str
    affected_files: tuple[str, ...] = ()
    exit_code: int | None = None
    timestamp: float = 0.0
    confidence: float = 1.0
    source: str = "tool-runtime"
    generation: int | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["affected_files"] = list(self.affected_files)
        return payload

    @classmethod
    def observed(
        cls,
        *,
        kind: str,
        command: str,
        scope: str,
        status: str,
        output: str,
        affected_files: tuple[str, ...] = (),
        exit_code: int | None = None,
        source: str = "tool-runtime",
        generation: int | None = None,
    ) -> "VerificationEvidence":
        passed = True if status == "passed" else (False if status == "failed" else None)
        return cls(
            kind=kind,
            command=command,
            scope=scope,
            status=status,
            passed=passed,
            output_reference=str(output or "").strip()[:800],
            affected_files=affected_files,
            exit_code=exit_code,
            timestamp=time.time(),
            source=source,
            generation=generation,
        )


__all__ = [
    "VerificationEvidence",
    "VerificationState",
    "is_environment_verification_failure",
]
