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


@pytest.mark.parametrize("stop", ["timeout", "abort", "tool-cancel"])
def test_http_wait_termination_reaches_tool_output_and_question_lifecycle(stop):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from nz_coder.http_service.interactions import InteractionBroker
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.tools import scoped_tool_cancellation
    from nz_coder.tools.question import scoped_question_lifecycle_reporter

    bus = SessionEventBus(session_id="question-timeout")
    broker = InteractionBroker(session_id=bus.session_id, event_bus=bus, timeout_seconds=.05 if stop == "timeout" else 2)
    broker.begin_run()
    lifecycle = []
    cancel = threading.Event()
    events = bus.subscribe()

    def ask():
        with (
            scoped_tool_cancellation(cancel),
            scoped_question_asker(broker.ask_question),
            scoped_question_lifecycle_reporter(lambda action, payload: lifecycle.append((action, payload))),
        ):
            return dispatch("question", {"questions": _questions()})

    try:
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(ask)
            asked = events.get(timeout=2)
            if stop == "abort":
                assert broker.cancel_all("aborted", block_new=True) == 1
            elif stop == "tool-cancel":
                cancel.set()
            result = waiting.result(timeout=2)
        ended = events.get(timeout=2)
        reason = "timeout" if stop == "timeout" else "cancelled"
        title = "Question timed out" if stop == "timeout" else "Question cancelled"
        assert ended.type == "question.rejected"
        assert ended.properties["reason"] == ("aborted" if stop == "abort" else reason)
        assert ended.properties["request_id"] == asked.properties["id"] == lifecycle[-1][1]["request_id"]
        assert [action for action, _ in lifecycle] == ["pending", "terminated"]
        assert lifecycle[-1][1]["reason"] == reason
        assert result == title + " without an answer."
        assert result.title == title
        assert result.metadata == {"answers": [], "reason": reason}
    finally:
        events.close()
        broker.close()


def test_callback_transport_timeout_remains_a_service_error():
    from nz_coder.tools.question import scoped_question_lifecycle_reporter

    def disconnected(_questions):
        raise TimeoutError("connection to UI failed")

    lifecycle = []
    with (
        scoped_question_asker(disconnected),
        scoped_question_lifecycle_reporter(lambda action, _payload: lifecycle.append(action)),
    ):
        result = dispatch("question", {"questions": _questions()})
    assert result.startswith("Error:")
    assert lifecycle == ["pending", "error"]


def test_question_after_abort_reports_cancelled_before_publication():
    from nz_coder.http_service.interactions import InteractionBroker
    from nz_coder.protocol.session_events import SessionEventBus

    bus = SessionEventBus(session_id="late-user-question")
    broker = InteractionBroker(session_id=bus.session_id, event_bus=bus)
    broker.begin_run()
    broker.cancel_all("aborted", block_new=True)
    with scoped_question_asker(broker.ask_question):
        result = dispatch("question", {"questions": _questions()})
    assert result.metadata == {"answers": [], "reason": "cancelled"}
    assert not bus.recent() and not broker.list()
    broker.close()
    with scoped_question_asker(broker.ask_question):
        result = dispatch("question", {"questions": _questions()})
    assert result == "Error: Interactive question service unavailable."


@pytest.mark.parametrize("submit", ["reply", "reject"])
def test_expired_question_rejects_input_before_timeout_worker_gets_lock(monkeypatch, submit):
    from concurrent.futures import ThreadPoolExecutor
    from nz_coder.http_service.interactions import InteractionBroker, InteractionNotFoundError
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.tools.question import QuestionWaitEnded

    bus = SessionEventBus(session_id="late-question")
    broker = InteractionBroker(session_id=bus.session_id, event_bus=bus, timeout_seconds=.2)
    events = bus.subscribe()
    broker.begin_run()
    try:
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(broker.ask_question, _questions())
            request = events.get(timeout=2).properties
            monkeypatch.setattr("nz_coder.http_service.interactions.time.time", lambda: request["expires_at"])
            with pytest.raises(InteractionNotFoundError):
                if submit == "reply":
                    broker.reply_question(request["id"], [["PostgreSQL"]])
                else:
                    broker.reject_question(request["id"])
            with pytest.raises(QuestionWaitEnded) as caught:
                waiting.result(timeout=2)
            assert caught.value.reason == "timeout"
        assert [event.type for event in bus.recent()] == ["question.asked", "question.rejected"]
        assert bus.recent()[-1].properties["reason"] == "timeout"
    finally:
        events.close()
        broker.close()


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
