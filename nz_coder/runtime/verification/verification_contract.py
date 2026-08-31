"""Run-local contracts for deterministic user-declared verification."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import shlex

from nz_coder.runtime.agent.task_policy import is_test_file, test_command_targets


_COMMAND_RE = re.compile(
    r"(?P<command>(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?pytest\b[^\n`，,。；;]*)",
    re.IGNORECASE,
)
_SHELL_COMPOSITION_RE = re.compile(r"(?:\|\||&&|[|><;$])")
_TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs"}


def _persisted_int(value: object, *, default: int) -> int:
    """Normalize one integer from a potentially damaged Session snapshot."""
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def effective_acceptance_generation(state) -> int:
    """Return the generation that can invalidate exact acceptance evidence."""
    if isinstance(state, Mapping):
        mutation = state.get("mutation_generation", 0)
        scoped = state.get("acceptance_mutation_generation")
    else:
        mutation = getattr(state, "mutation_generation", 0)
        scoped = getattr(state, "acceptance_mutation_generation", None)
    value = mutation if scoped is None else scoped
    return max(0, int(value or 0))


@dataclass
class VerificationContract:
    """One safe pytest command and its latest mutation-scoped evidence."""

    command: str
    targets: tuple[str, ...]
    attempted_generation: int = -1
    attempts: int = 0
    passed: bool | None = None
    output: str = ""
    source: str = ""
    zone: str = ""

    def matches_command(self, command: str) -> bool:
        """Return whether *command* is token-equivalent to this contract."""
        try:
            expected = shlex.split(self.command, posix=True)
            observed = shlex.split(str(command or ""), posix=True)
        except ValueError:
            return False
        return bool(expected and observed == expected)

    def is_due(
        self,
        *,
        zone: str,
        has_diff: bool,
        mutation_generation: int,
    ) -> bool:
        """Return whether convergence should execute this contract now."""
        return bool(
            zone in {"red", "completion"}
            and has_diff
            and int(mutation_generation) > self.attempted_generation
        )

    def record_attempt(
        self,
        generation: int,
        *,
        passed: bool,
        output: str,
        source: str = "",
        zone: str = "",
    ) -> None:
        """Record one settled attempt for a workspace mutation generation."""
        self.attempted_generation = int(generation)
        self.attempts += 1
        self.passed = bool(passed)
        self.output = str(output)[-8000:]
        self.source = str(source or "")
        self.zone = str(zone or "")

    def to_dict(self) -> dict:
        """Return a JSON-safe persisted representation."""
        return {
            "command": self.command,
            "targets": list(self.targets),
            "attempted_generation": self.attempted_generation,
            "attempts": self.attempts,
            "passed": self.passed,
            "output": self.output,
            "source": self.source,
            "zone": self.zone,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "VerificationContract":
        """Restore a contract from persisted RuntimeState data."""
        if not isinstance(value, dict):
            raise ValueError("verification contract must be an object")
        raw_targets = value.get("targets") or ()
        targets = raw_targets if isinstance(raw_targets, (list, tuple)) else ()
        return cls(
            command=str(value.get("command") or ""),
            targets=tuple(str(item) for item in targets),
            attempted_generation=_persisted_int(
                value.get("attempted_generation"),
                default=-1,
            ),
            attempts=max(
                0,
                _persisted_int(value.get("attempts"), default=0),
            ),
            passed=(
                bool(value["passed"])
                if value.get("passed") is not None
                else None
            ),
            output=str(value.get("output") or ""),
            source=str(value.get("source") or ""),
            zone=str(value.get("zone") or ""),
        )


def extract_verification_contract(text: str) -> VerificationContract | None:
    """Extract one bounded workspace-relative pytest command from user text."""
    for match in _COMMAND_RE.finditer(text or ""):
        command = match.group("command").strip()
        if not command or _SHELL_COMPOSITION_RE.search(command):
            continue
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            continue
        # English prose often follows an unquoted command in the same line.
        # Walk back to the longest prefix whose positional arguments are all
        # explicit test paths; this keeps the command while excluding prose.
        for end in range(len(tokens), 0, -1):
            candidate = list(tokens[:end])
            candidate[-1] = candidate[-1].rstrip(".!?")
            if not candidate[-1]:
                continue
            normalized = " ".join(shlex.quote(token) for token in candidate)
            targets = test_command_targets(normalized)
            if targets and all(_looks_like_test_scope(item) for item in targets):
                return VerificationContract(command=normalized, targets=targets)
    return None


def _looks_like_test_scope(path: str) -> bool:
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    return bool(
        is_test_file(path)
        or any(part in _TEST_DIR_NAMES for part in parts)
    )


__all__ = ["VerificationContract", "extract_verification_contract"]
