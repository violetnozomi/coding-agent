"""Behavioral contracts for immutable Agent runtime profiles."""
from __future__ import annotations

import pytest

from nz_coder.runtime.core.profiles import (
    BACKGROUND_PROFILE,
    MAIN_PROFILE,
    READ_CHILD_PROFILE,
    WORKFLOW_PROFILE,
    WRITE_CHILD_PROFILE,
    RunMode,
    RunProfile,
    profile_for_mode,
)


def test_read_child_profile_cannot_mutate_spawn_or_prompt() -> None:
    """A read child must remain safe when selected by orchestration code."""
    assert READ_CHILD_PROFILE.allow_mutation is False
    assert READ_CHILD_PROFILE.allow_child_agents is False
    assert READ_CHILD_PROFILE.interactive_questions is False


def test_main_and_write_profiles_expose_distinct_capabilities() -> None:
    """Main interaction must not leak into a scoped write child."""
    assert MAIN_PROFILE.allow_mutation is True
    assert MAIN_PROFILE.allow_child_agents is True
    assert MAIN_PROFILE.interactive_questions is True
    assert WRITE_CHILD_PROFILE.allow_mutation is True
    assert WRITE_CHILD_PROFILE.allow_child_agents is False
    assert WRITE_CHILD_PROFILE.interactive_questions is False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (RunMode.MAIN, MAIN_PROFILE),
        (RunMode.READ_CHILD, READ_CHILD_PROFILE),
        (RunMode.WRITE_CHILD, WRITE_CHILD_PROFILE),
        (RunMode.BACKGROUND, BACKGROUND_PROFILE),
        (RunMode.WORKFLOW, WORKFLOW_PROFILE),
    ],
)
def test_profile_for_mode_is_exhaustive(mode: RunMode, expected: RunProfile) -> None:
    """Every supported execution surface must resolve to one canonical policy."""
    assert profile_for_mode(mode) is expected


def test_profile_for_mode_accepts_wire_value() -> None:
    """Host adapters may pass a serialized mode without rebuilding policy."""
    assert profile_for_mode("read_child") is READ_CHILD_PROFILE


def test_profile_rejects_empty_name() -> None:
    """An unnamed profile cannot be diagnosed in trace or session metadata."""
    with pytest.raises(ValueError, match="name"):
        RunProfile(name="", mode=RunMode.MAIN)


def test_profile_rejects_unknown_mode() -> None:
    """Typos in host configuration must fail before an Agent starts."""
    with pytest.raises(ValueError, match="Unknown run mode"):
        profile_for_mode("reader")
