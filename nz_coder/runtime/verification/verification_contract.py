"""Run-local contracts for deterministic user-declared verification."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import shlex

from nz_coder.runtime.agent.task_policy import (
    _declared_command_spans, _python_entry,
    canonical_unittest_discovery, is_test_file, test_command_targets, unittest_selectors,
)


_SHELL_COMPOSITION_RE = re.compile(r"(?:\|\||&&|[|><;$])")
_TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs"}
_EXPLICIT_COMMAND_RE = re.compile(
    r"(?:acceptance\s+(?:command|check)|(?:验收|验证)命令)(?:\s*(?:is|为|是))?\s*[:：]?\s*`([^`\n]+)`",
    re.IGNORECASE,
)


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


@dataclass(frozen=True)
class VerificationContract:
    """One safe declared test command and its latest mutation-scoped evidence."""

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
    """主命令投影；完整验收集合由复数接口交给统一验证域。"""
    return next(iter(extract_verification_contracts(text)), None)


def declared_acceptance(state) -> tuple[dict, ...]:
    """读取域的全量验收投影；只有定义而没有事实时一律展示待确认。"""
    if isinstance(state, dict):
        verification = state.get("verification") or {}
        contract = state.get("task_contract") or {}
    else:
        domain = state.verification_domain
        if domain is not None:
            return domain.snapshot().acceptance_checks
        verification, contract = {}, state.task_contract
    if "acceptance_checks" in verification:
        return tuple(verification["acceptance_checks"])
    return tuple({"command": command, "cwd": "", "status": "pending", "generation": None, "output": ""}
                 for command in contract.get("acceptance_commands", ()))


def extract_verification_contracts(text: str) -> tuple[VerificationContract, ...]:
    """显式验收保留原命令；普通正向指令只采纳完整可解释的原生选择。"""
    value = str(text or "")
    explicit = list(_EXPLICIT_COMMAND_RE.finditer(value))
    contracts: dict[str, VerificationContract] = {}
    for position, command in _declared_command_spans(value):
        declared = any(match.start() <= position < match.end() for match in explicit)
        if not command or (not declared and _SHELL_COMPOSITION_RE.search(command)):
            continue
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        if declared:
            contracts.setdefault(command, VerificationContract(command=command, targets=test_command_targets(command)))
            continue
        native_unittest = unittest_selectors(tokens)
        entry = _python_entry(tokens)
        native_pytest = tokens[0] in {"pytest", "py.test", "pytest3"} or entry is not None and entry[2] and entry[0] in {"pytest", "py.test"}
        native_node = tokens[:2] == ["node", "--test"]
        if native_unittest is None and not native_pytest and not native_node:
            continue
        targets = test_command_targets(command)
        # discover 以完整集合命令确定身份，不应伪造文件选择器或漏掉明确验收。
        if not targets and (native_unittest is None or canonical_unittest_discovery(command) is None):
            continue
        if native_unittest is None and not native_node and not all(_looks_like_test_scope(item) for item in targets):
            continue
        contracts.setdefault(command, VerificationContract(command=command, targets=targets))
    return tuple(contracts.values())


def _looks_like_test_scope(path: str) -> bool:
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    return bool(
        is_test_file(path)
        or any(part in _TEST_DIR_NAMES for part in parts)
    )


__all__ = ["VerificationContract", "extract_verification_contract", "extract_verification_contracts"]
