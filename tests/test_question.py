"""Tests for structured user questions and the terminal adapter."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.interface.questions import _parse_answer, build_terminal_question_asker
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.agent.subagent import _subagent_tools
from nz_coder.tools import dispatch, get_execution_mode, get_specs
from nz_coder.tools.question import scoped_question_asker
from nz_coder.tools import ToolOutput


def _questions(*, multiple: bool = False) -> list[dict]:
    return [{
        "header": "Storage",
        "question": "Which storage backend should be used?",
        "multiple": multiple,
        "options": [
            {"label": "SQLite (Recommended)", "description": "Local and dependency-free."},
            {"label": "PostgreSQL", "description": "Requires an external service."},
        ],
    }]


def test_question_tool_is_registered_as_serial():
    names = {item["function"]["name"] for item in get_specs()}

    assert "question" in names
    assert get_execution_mode("question") == "serial"


def test_question_is_safe_for_parent_but_hidden_from_subagents():
    decision = PermissionManager("default").check("question", {"questions": _questions()})
    child_names = {
        item["function"]["name"]
        for item in _subagent_tools("general-purpose")
    }

    assert decision["behavior"] == "allow"
    assert "question" not in child_names


def test_question_rejects_invalid_counts_and_duplicate_labels():
    assert dispatch("question", {"questions": []}).startswith("Error:")
    invalid = _questions()
    invalid[0]["options"][1]["label"] = "sqlite (recommended)"

    result = dispatch("question", {"questions": invalid})

    assert result == "Error: question 1 option labels must be unique"


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        (_questions() * 5, "between 1 and 4"),
        ([{**_questions()[0], "options": _questions()[0]["options"][:1]}], "between 2 and 5"),
        ([{**_questions()[0], "options": _questions()[0]["options"] * 3}], "between 2 and 5"),
        ([{**_questions()[0], "header": "header-is-too-long"}], "12 characters or fewer"),
        ([{**_questions()[0], "multiple": "yes"}], "multiple must be a boolean"),
    ],
)
def test_question_validates_schema_boundaries(questions, message):
    assert message in dispatch("question", {"questions": questions})


def test_question_without_interactive_service_does_not_block():
    result = dispatch("question", {"questions": _questions()})

    assert result.startswith("Error: Interactive question service unavailable")


def test_question_uses_context_local_service_and_formats_answers():
    seen = []

    def asker(questions):
        seen.extend(questions)
        return [["SQLite (Recommended)"]]

    with scoped_question_asker(asker):
        result = dispatch("question", {"questions": _questions()})

    assert seen[0]["header"] == "Storage"
    assert result == (
        'User has answered your questions: "Which storage backend should be used?"='
        '"SQLite (Recommended)". You can now continue with the user\'s answers in mind.'
    )
    assert isinstance(result, ToolOutput)
    assert result.title == "Asked 1 question"
    assert result.metadata == {"answers": [["SQLite (Recommended)"]]}
    assert dispatch("question", {"questions": _questions()}).startswith("Error:")


def test_question_services_are_isolated_between_async_contexts():
    async def run(label):
        with scoped_question_asker(lambda _questions: [[label]]):
            await asyncio.sleep(0)
            return dispatch("question", {"questions": _questions()})

    async def scenario():
        return await asyncio.gather(run("alpha"), run("beta"))

    first, second = asyncio.run(scenario())

    assert '"alpha"' in first and '"beta"' not in first
    assert '"beta"' in second and '"alpha"' not in second


def test_question_dismissal_is_a_normal_tool_result():
    with scoped_question_asker(lambda _questions: None):
        result = dispatch("question", {"questions": _questions()})

    assert result.startswith("User dismissed the question.")
    assert not result.startswith("Error:")
    assert isinstance(result, ToolOutput)
    assert result.title == "Question dismissed"
    assert result.metadata == {"answers": [], "dismissed": True}


def test_question_rejects_malformed_service_answers():
    with scoped_question_asker(lambda _questions: [[{"not": "text"}]]):
        result = dispatch("question", {"questions": _questions()})

    assert result == "Error: Interactive question service returned malformed answers"


def test_parse_answer_supports_single_multiple_and_custom_text():
    single = _questions()[0]
    multiple = _questions(multiple=True)[0]

    assert _parse_answer("2", single) == ["PostgreSQL"]
    assert _parse_answer("1,2,1", multiple) == ["SQLite (Recommended)", "PostgreSQL"]
    assert _parse_answer("Use the existing database", single) == ["Use the existing database"]
    assert _parse_answer("9", single) == []
    assert _parse_answer("", single) is None


class _FakeConsole:
    def __init__(self, answers):
        self.answers = list(answers)
        self.lines = []

    def print(self, value, **_kwargs):
        self.lines.append(value)

    def input(self, _prompt, **_kwargs):
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _FakeRenderer:
    def __init__(self, answers):
        self.console = _FakeConsole(answers)
        self.paused = 0
        self.resumed = 0

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1


def test_terminal_question_asker_pauses_renderer_and_recovers_invalid_choice():
    renderer = _FakeRenderer(["9", "1"])
    asker = build_terminal_question_asker(renderer)

    result = asker(_questions())

    assert result == [["SQLite (Recommended)"]]
    assert renderer.paused == 1
    assert renderer.resumed == 1
    assert any("Invalid selection" in line for line in renderer.console.lines)


def test_terminal_question_asker_resumes_renderer_after_eof():
    renderer = _FakeRenderer([EOFError()])
    asker = build_terminal_question_asker(renderer)

    assert asker(_questions()) is None
    assert renderer.paused == 1
    assert renderer.resumed == 1
