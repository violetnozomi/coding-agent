"""Tests for RuntimeState persistence and acceptance criteria."""
from __future__ import annotations


def test_runtime_state_persists_active_state(tmp_path):
    from nz_coder.runtime_state import RuntimeState

    path = tmp_path / "runtime_state.json"
    state = RuntimeState()
    state.reset(max_turns=12, timeout_seconds=90)
    state.turn_count = 4
    state.edits_this_run = 1
    state.has_diff = True
    state.diff_chars = 300
    state.changed_files = ["app.py"]
    state.acceptance_criteria = ["tests/test_app.py::test_bug"]
    state.save(path, active=True)

    restored = RuntimeState()
    assert restored.load(path) is True
    assert restored.turn_count == 4
    assert restored.has_diff is True
    assert restored.task_complexity() == "L1"
    assert restored.acceptance_criteria == ["tests/test_app.py::test_bug"]

    restored.save(path, active=False)
    inactive = RuntimeState()
    assert inactive.load(path) is False


def test_acceptance_criteria_extracts_tests_and_should_sentences():
    from nz_coder.runtime_state import extract_acceptance_criteria

    text = (
        "FAILED tests/test_dates.py::test_http_date_tz\n"
        "HTTP date parsing should preserve timezone offsets."
    )

    criteria = extract_acceptance_criteria(text)

    assert "tests/test_dates.py::test_http_date_tz" in criteria
    assert any("should preserve timezone" in item for item in criteria)


def test_l0_runtime_state_omits_routine_prompt_block():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=50)
    state.turn_count = 1

    assert state.task_complexity() == "L0"
    assert state.build_prompt_block() == ""


def test_runtime_state_discuss_mode_does_not_push_source_edit():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("How should we design this API?")
    state.turn_count = 12

    block = state.build_prompt_block()

    assert "task_mode=discuss" in block
    assert "No source edit" not in block
    assert "smallest relevant code change" not in block


def test_runtime_state_tests_modified_is_neutral_when_requested():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("add unit tests for parser")
    state.turn_count = 2
    state.has_diff = True
    state.diff_chars = 500
    state.changed_files = ["src/parser.py", "tests/test_parser.py"]
    state.tests_modified = True

    block = state.build_prompt_block()

    assert "task_mode=test" in block
    assert "test updates requested" in block
    assert "confirm this matches" not in block
