"""Context-local runtime overrides for concurrent agent execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

from nz_coder.foundation import config


@dataclass(frozen=True)
class RuntimeOverrides:
    """Per-execution settings that may differ between concurrent agents."""

    max_agent_turns: int | None = None
    nominal_agent_turns: int | None = None
    agent_timeout_seconds: float | None = None
    max_parallel_tasks: int | None = None
    strict_local_tools: bool | None = None
    repo_intelligence_mode: str | None = None
    repo_retrieval_strategy: str | None = None


_RUNTIME_OVERRIDES: ContextVar[RuntimeOverrides] = ContextVar(
    "nz_coder_runtime_overrides",
    default=RuntimeOverrides(),
)
_BROAD_TESTS_BLOCKED: ContextVar[bool] = ContextVar(
    "nz_coder_broad_tests_blocked",
    default=False,
)
_DECLARED_TEST_SCOPES: ContextVar[tuple[str, ...]] = ContextVar(
    "nz_coder_declared_test_scopes",
    default=(),
)


def max_agent_turns() -> int:
    """Return the current execution's agent-turn limit."""
    value = _RUNTIME_OVERRIDES.get().max_agent_turns
    return max(1, int(config.MAX_AGENT_TURNS if value is None else value))


def nominal_agent_turns() -> int:
    """Return the convergence SLA without exceeding the execution hard cap."""
    value = _RUNTIME_OVERRIDES.get().nominal_agent_turns
    if value is None:
        value = getattr(config, "NOMINAL_AGENT_TURNS", 15)
    return min(max_agent_turns(), max(1, int(value)))


def agent_timeout_seconds() -> float:
    """Return the current execution's total agent timeout, or zero if disabled."""
    value = _RUNTIME_OVERRIDES.get().agent_timeout_seconds
    if value is None:
        value = getattr(config, "AGENT_TIMEOUT_SECONDS", 0)
    return max(0.0, float(value or 0))


def max_parallel_tasks() -> int:
    """Return the current execution's scheduler concurrency limit."""
    value = _RUNTIME_OVERRIDES.get().max_parallel_tasks
    return max(1, int(config.MAX_PARALLEL_TASKS if value is None else value))


def strict_local_tools() -> bool:
    """Return whether this execution forbids network-capable tool behavior."""
    return bool(_RUNTIME_OVERRIDES.get().strict_local_tools)


def repo_intelligence_mode() -> str:
    """Return the benchmark/runtime retrieval tier for repository tools."""
    return str(_RUNTIME_OVERRIDES.get().repo_intelligence_mode or "lookup")


def repo_retrieval_strategy() -> str:
    """Return deterministic retrieval behavior for this execution."""
    return str(_RUNTIME_OVERRIDES.get().repo_retrieval_strategy or "guidance")


def broad_tests_blocked() -> bool:
    """Return whether broad test runners are blocked in this execution."""
    return _BROAD_TESTS_BLOCKED.get()


def set_broad_tests_blocked(blocked: bool) -> None:
    """Update the current execution's broad-test guard."""
    _BROAD_TESTS_BLOCKED.set(bool(blocked))


def declared_test_scopes() -> tuple[str, ...]:
    """Return test path scopes explicitly declared by the current user."""
    return _DECLARED_TEST_SCOPES.get()


def set_declared_test_scopes(scopes: tuple[str, ...]) -> None:
    """Replace the current run's user-declared verification scopes."""
    _DECLARED_TEST_SCOPES.set(tuple(str(scope) for scope in scopes if str(scope)))


@contextmanager
def scoped_runtime_overrides(
    *,
    max_agent_turns: int | None = None,
    nominal_agent_turns: int | None = None,
    agent_timeout_seconds: float | None = None,
    max_parallel_tasks: int | None = None,
    strict_local_tools: bool | None = None,
    repo_intelligence_mode: str | None = None,
    repo_retrieval_strategy: str | None = None,
):
    """Temporarily override runtime settings without mutating global config."""
    current = _RUNTIME_OVERRIDES.get()
    updated = replace(
        current,
        max_agent_turns=(
            current.max_agent_turns
            if max_agent_turns is None
            else max(1, int(max_agent_turns))
        ),
        nominal_agent_turns=(
            current.nominal_agent_turns
            if nominal_agent_turns is None
            else max(1, int(nominal_agent_turns))
        ),
        agent_timeout_seconds=(
            current.agent_timeout_seconds
            if agent_timeout_seconds is None
            else max(0.0, float(agent_timeout_seconds))
        ),
        max_parallel_tasks=(
            current.max_parallel_tasks
            if max_parallel_tasks is None
            else max(1, int(max_parallel_tasks))
        ),
        strict_local_tools=(
            current.strict_local_tools
            if strict_local_tools is None
            else bool(strict_local_tools)
        ),
        repo_intelligence_mode=(
            current.repo_intelligence_mode
            if repo_intelligence_mode is None
            else str(repo_intelligence_mode)
        ),
        repo_retrieval_strategy=(
            current.repo_retrieval_strategy
            if repo_retrieval_strategy is None
            else str(repo_retrieval_strategy)
        ),
    )
    token = _RUNTIME_OVERRIDES.set(updated)
    try:
        yield updated
    finally:
        _RUNTIME_OVERRIDES.reset(token)


@contextmanager
def scoped_broad_test_guard(blocked: bool = False):
    """Bind an independent broad-test guard for one agent or child agent."""
    token = _BROAD_TESTS_BLOCKED.set(bool(blocked))
    try:
        yield
    finally:
        _BROAD_TESTS_BLOCKED.reset(token)


@contextmanager
def scoped_declared_test_scopes(scopes: tuple[str, ...] = ()):
    """Bind independent user-declared verification scopes for one execution."""
    token = _DECLARED_TEST_SCOPES.set(
        tuple(str(scope) for scope in scopes if str(scope))
    )
    try:
        yield
    finally:
        _DECLARED_TEST_SCOPES.reset(token)
