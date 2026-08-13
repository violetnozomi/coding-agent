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



def test_acceptance_criteria_extracts_numbered_requirements():
    from nz_coder.runtime_state import extract_acceptance_criteria

    text = (
        "1. 用户注册时校验 email 格式\n"
        "2. 新增 update_user(user_id, **kwargs) 函数\n"
        "3. 写 test_user_manager.py，覆盖正常流程和异常情况"
    )

    criteria = extract_acceptance_criteria(text)

    assert any("校验 email 格式" in item for item in criteria)
    assert any("update_user" in item for item in criteria)
    assert any("test_user_manager.py" in item for item in criteria)

def test_l0_runtime_state_omits_routine_prompt_block():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=50)
    state.turn_count = 1

    assert state.task_complexity() == "L0"
    assert state.build_prompt_block() == ""


def test_runtime_state_keeps_all_small_numbered_requirements_visible():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text(
        "1. 修改 api/user.py\n2. 新增 tests/test_user_api.py\n3. 更新 docs/user.md\n4. 写回归测试覆盖异常分支"
    )
    state.turn_count = 2

    block = state.build_prompt_block()

    assert "Acceptance criteria (4)" in block
    assert "写回归测试覆盖异常分支" in block


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



def test_runtime_state_warns_when_task_requests_tests_but_none_changed():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("在 test/user_manger.py 的基础上新增 update_user，并写 test_user_manager.py")
    state.turn_count = 3
    state.has_diff = True
    state.diff_chars = 400
    state.changed_files = ["test/user_manger.py"]
    state.tests_modified = False

    block = state.build_prompt_block()

    assert "User named target files" in block
    assert "test/user_manger.py" in block
    assert "no test files have been changed yet" in block.lower()

def test_runtime_state_project_creation_guides_greenfield_flow():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("帮我创建一个 FastAPI todo API 项目")
    state.turn_count = 6

    block = state.build_prompt_block()

    assert "analyze_project_requirements -> create_project_blueprint -> scaffold_project" in block
    assert "Do not start with grep_search unless you are intentionally reusing local code." in block


def test_strict_progress_emits_one_nudge_after_twelve_investigations():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the parser bug")
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": f"token-{index}"}, "match")

    first = state.build_prompt_block(strict=True)
    second = state.build_prompt_block(strict=True)

    assert "STRICT CONVERGENCE" in first
    assert "12 investigation calls" in first
    assert "STRICT CONVERGENCE" not in second


def test_strict_progress_blocks_more_investigation_at_hard_limit():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    for index in range(20):
        state.observe_tool("read_file", {"path": f"file-{index}.py"}, "source")

    assert state.strict_progress_action("grep_search") == "block"
    assert state.strict_progress_action("read_symbol") == "block"
    assert state.strict_progress_action("edit_file") == "allow"
    assert state.strict_progress_action("diff_status") == "allow"


def test_successful_write_resets_strict_investigation_generation():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": f"token-{index}"}, "match")
    state.build_prompt_block(strict=True)

    state.observe_tool("edit_file", {"path": "parser.py"}, "Done")

    assert state.investigation_calls_since_edit == 0
    assert state.mutation_generation == 1
    assert state.strict_progress_nudges == 0
    assert state.strict_progress_blocks == 0
    assert state.strict_progress_action("grep_search") == "allow"


def test_strict_terminal_evidence_is_order_independent_within_generation():
    """Regression: verify-before-diff must settle the same mutation generation."""
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_tool("verify_changed_files", {}, "OK: py_compile changed files")

    assert state.strict_generation_terminal_ready() is False

    state.observe_tool(
        "diff_status",
        {},
        "\n".join([
            "has_non_empty_diff: true",
            "diff_chars: 20",
            "tests_modified: false",
            "source_only: true",
            "Changed files:",
            "  app.py",
        ]),
    )

    assert state.strict_generation_terminal_ready() is True


def test_new_mutation_invalidates_previous_generation_terminal_evidence():
    """A verification from generation one cannot finalize generation two."""
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_tool("diff_status", {}, "has_non_empty_diff: true\ntests_modified: false\nsource_only: true")
    state.observe_tool("verify_changed_files", {}, "OK: passed")
    assert state.strict_generation_terminal_ready() is True

    state.observe_tool("edit_file", {"path": "app.py"}, "Done")

    assert state.strict_generation_terminal_ready() is False


def test_requested_test_changes_can_settle_strict_generation():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.wants_tests = True
    state.observe_tool("edit_file", {"path": "pkg/app.py"}, "Done")
    state.observe_tool(
        "diff_status",
        {},
        "\n".join([
            "has_non_empty_diff: true",
            "tests_modified: true",
            "source_only: false",
            "Changed files:",
            "  pkg/app.py",
            "  tests/test_app.py",
        ]),
    )
    state.observe_tool("verify_changed_files", {}, "OK: passed")

    assert state.diff_generation == state.mutation_generation
    assert state.strict_generation_terminal_ready() is True


def test_unrequested_test_changes_do_not_settle_strict_generation():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.wants_tests = False
    state.observe_tool("edit_file", {"path": "tests/test_app.py"}, "Done")
    state.observe_tool(
        "diff_status",
        {},
        "has_non_empty_diff: true\ntests_modified: true\nsource_only: false",
    )
    state.observe_tool("verify_changed_files", {}, "OK: passed")

    assert state.diff_generation == -1
    assert state.strict_generation_terminal_ready() is False


def test_read_only_bash_source_inspection_consumes_strict_investigation_budget():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("bash", {"command": "grep -n token pkg/app.py"}, "match")
    state.observe_tool("bash", {"command": "cat pkg/app.py"}, "source")

    assert state.investigation_calls_since_edit == 2
    state.investigation_calls_since_edit = 20
    assert state.strict_progress_action(
        "bash", tool_input={"command": "head -20 pkg/app.py"}
    ) == "block"


def test_bash_status_and_verification_do_not_consume_investigation_budget():
    from nz_coder.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("bash", {"command": "git status --short"}, "")
    state.observe_tool("bash", {"command": "python3 -m py_compile pkg/app.py"}, "")

    assert state.investigation_calls_since_edit == 0
