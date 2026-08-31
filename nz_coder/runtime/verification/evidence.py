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
    re.compile(
        r"^(?:(?:E\s+)?(?:ModuleNotFoundError|ImportError):\s*|"
        r"[^\r\n:]*python(?:\d+(?:\.\d+)*)?:\s*)"
        r"No module named ['\"]?(?:pytest|tox|nox|ruff|mypy|pyright)['\"]?\s*$",
        re.I | re.M,
    ),
    re.compile(
        r"^(?:(?:ERROR|ImportError|ModuleNotFoundError):\s*)?"
        r"(?:error importing|could not import|failed to load) "
        r"(?:pytest )?plugin\b",
        re.I | re.M,
    ),
)
_STDLIB_COMPAT_IMPORT_FAILURE = re.compile(
    r"^(?:E\s+)?ImportError:\s+cannot import name ['\"][^'\"]+['\"] from "
    r"['\"](?:collections|inspect|asyncio|typing)['\"]",
    re.I | re.M,
)
_MODULE_NOT_FOUND_FAILURE = re.compile(
    r"^(?:E\s+)?ModuleNotFoundError:\s+No module named ['\"][^'\"]+['\"]\s*$",
    re.I | re.M,
)
_CHAINED_MODULE_NOT_FOUND_WRAPPER = re.compile(
    r"^(?:RuntimeError|ImportError):\s+[^\r\n]*\bmodule not found\b[^\r\n]*$",
    re.I | re.M,
)
_PYTEST_HOST_CONFIG_MISMATCH = re.compile(
    r"^ERROR:\s+while parsing the following warning configuration:.*$.*"
    r"^(?:E\s+)?AttributeError:\s+module ['\"]pytest['\"] has no attribute",
    re.I | re.M | re.S,
)
_PYTEST_CONFIG_FILES = frozenset({"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"})


def _normalized_changed_paths(
    changed_files: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Normalize run-local path spelling for traceback attribution."""
    normalized: list[str] = []
    for value in changed_files:
        path = str(value or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path:
            normalized.append(path)
    return tuple(normalized)


def _strict_startup_traceback_is_external(
    body: str,
    changed_files: tuple[str, ...] | list[str],
) -> bool:
    """Return whether a startup traceback reports an external missing module."""
    stripped = str(body or "").strip()
    if not stripped.startswith("Traceback (most recent call last):"):
        return False
    match = _MODULE_NOT_FOUND_FAILURE.search(stripped)
    if match is None:
        return False
    if match.end() != len(stripped):
        wrapper = _CHAINED_MODULE_NOT_FOUND_WRAPPER.search(stripped, match.end())
        chain = stripped[match.end():wrapper.start()] if wrapper is not None else ""
        if (
            wrapper is None
            or wrapper.end() != len(stripped)
            or "direct cause of the following exception" not in chain.casefold()
        ):
            return False
    normalized_output = stripped.replace("\\", "/")
    return not any(
        path in normalized_output
        for path in _normalized_changed_paths(changed_files)
    )


def is_environment_verification_failure(
    command: str,
    output: str,
    changed_files: tuple[str, ...] | list[str] = (),
    *,
    strict_offline: bool = False,
    exit_code: int | None = None,
) -> bool:
    """Separate unavailable verification infrastructure from code failures."""
    del command  # The child-controlled command/output text is not exit metadata.
    try:
        trusted_exit = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError, OverflowError):
        trusted_exit = None
    if trusted_exit is None:
        return False

    prefix = f"Command exited with code {trusted_exit}"
    if not str(output or "").startswith(prefix):
        return False
    body = str(output or "")[len(prefix):].lstrip("\r\n")
    if trusted_exit in {126, 127}:
        return True

    # Exit 1 is the ordinary "tests failed" category.  The only safe
    # environment exceptions are interpreter startup failures whose complete
    # body proves that the requested runner or an unchanged startup module
    # could not import.
    if trusted_exit == 1:
        return bool(
            _ENVIRONMENT_FAILURES[0].fullmatch(body.strip())
            or (
                strict_offline
                and _strict_startup_traceback_is_external(body, changed_files)
            )
        )
    if trusted_exit not in {2, 3, 4}:
        return False

    if (
        re.search(r"^ERROR collecting\b", body, re.I | re.M)
        and _STDLIB_COMPAT_IMPORT_FAILURE.search(body)
    ):
        normalized_output = body.replace("\\", "/")
        if not any(
            path in normalized_output
            for path in _normalized_changed_paths(changed_files)
        ):
            return True
    if strict_offline and _MODULE_NOT_FOUND_FAILURE.search(body):
        normalized_output = body.replace("\\", "/")
        if not any(
            path in normalized_output
            for path in _normalized_changed_paths(changed_files)
        ):
            return True
    if strict_offline and _PYTEST_HOST_CONFIG_MISMATCH.search(body):
        normalized_changed = _normalized_changed_paths(changed_files)
        changed_config = any(
            path.rsplit("/", 1)[-1] in _PYTEST_CONFIG_FILES
            for path in normalized_changed
        )
        normalized_output = body.replace("\\", "/")
        changed_path_in_output = any(
            path in normalized_output for path in normalized_changed
        )
        if not changed_config and not changed_path_in_output:
            return True
    return any(pattern.search(body) for pattern in _ENVIRONMENT_FAILURES)


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
