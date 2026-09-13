"""Structured user-question tool with context-local interaction injection."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Callable
import uuid

from nz_coder.protocol.public_error import format_public_error
from nz_coder.tools import ToolOutput, current_tool_call_id, register


QuestionAsker = Callable[[list[dict]], list[list[str]] | None]
QuestionLifecycleReporter = Callable[[str, dict], None]

_QUESTION_ASKER: ContextVar[QuestionAsker | None] = ContextVar(
    "nz_coder_question_asker",
    default=None,
)
_QUESTION_LIFECYCLE_REPORTER: ContextVar[
    QuestionLifecycleReporter | None
] = ContextVar(
    "nz_coder_question_lifecycle_reporter",
    default=None,
)
_QUESTION_REQUEST_ID: ContextVar[str] = ContextVar(
    "nz_coder_question_request_id",
    default="",
)


@contextmanager
def scoped_question_asker(asker: QuestionAsker | None):
    """Bind the interactive question service to one agent execution context."""
    token = _QUESTION_ASKER.set(asker)
    try:
        yield asker
    finally:
        _QUESTION_ASKER.reset(token)


@contextmanager
def scoped_question_lifecycle_reporter(reporter: QuestionLifecycleReporter):
    """Bind the durable QuestionPart sink for one Agent execution scope."""
    if not callable(reporter):
        raise ValueError("Question lifecycle reporter must be callable")
    token = _QUESTION_LIFECYCLE_REPORTER.set(reporter)
    try:
        yield
    finally:
        _QUESTION_LIFECYCLE_REPORTER.reset(token)


@contextmanager
def _scoped_question_request_id(request_id: str):
    token = _QUESTION_REQUEST_ID.set(str(request_id))
    try:
        yield
    finally:
        _QUESTION_REQUEST_ID.reset(token)


def current_question_request_id() -> str:
    """Return the request identity shared with the active interaction service."""
    return _QUESTION_REQUEST_ID.get()


def _report_lifecycle(action: str, payload: dict) -> None:
    reporter = _QUESTION_LIFECYCLE_REPORTER.get()
    if reporter is None:
        return
    try:
        reporter(str(action), dict(payload))
    except Exception:
        return


def _normalize_questions(questions) -> list[dict] | str:
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        return "questions must contain between 1 and 4 items"

    normalized: list[dict] = []
    for index, item in enumerate(questions, 1):
        if not isinstance(item, dict):
            return f"question {index} must be an object"
        question = str(item.get("question") or "").strip()
        header = str(item.get("header") or "").strip()
        options = item.get("options")
        multiple = item.get("multiple", False)
        if not question:
            return f"question {index} must include non-empty question text"
        if not header:
            return f"question {index} must include a non-empty header"
        if len(header) > 12:
            return f"question {index} header must be 12 characters or fewer"
        if not isinstance(multiple, bool):
            return f"question {index} multiple must be a boolean"
        if not isinstance(options, list) or not 2 <= len(options) <= 5:
            return f"question {index} options must contain between 2 and 5 items"

        normalized_options: list[dict] = []
        labels: set[str] = set()
        for option_index, option in enumerate(options, 1):
            if not isinstance(option, dict):
                return f"question {index} option {option_index} must be an object"
            label = str(option.get("label") or "").strip()
            description = str(option.get("description") or "").strip()
            if not label or not description:
                return (
                    f"question {index} option {option_index} must include "
                    "non-empty label and description"
                )
            key = label.casefold()
            if key in labels:
                return f"question {index} option labels must be unique"
            labels.add(key)
            normalized_options.append({"label": label, "description": description})

        normalized.append({
            "question": question,
            "header": header,
            "options": normalized_options,
            "multiple": multiple,
        })
    return normalized


def question(questions: list) -> str:
    """Ask one to four structured questions through the active UI service."""
    try:
        normalized = _normalize_questions(questions)
        if isinstance(normalized, str):
            return f"Error: {normalized}"
        asker = _QUESTION_ASKER.get()
        if asker is None:
            return (
                "Error: Interactive question service unavailable. Continue with a "
                "reasonable assumption or explain which user decision is required."
            )
        request_id = f"question-{uuid.uuid4().hex}"
        lifecycle = {
            "request_id": request_id,
            "tool_call_id": current_tool_call_id(),
            "questions": normalized,
        }
        _report_lifecycle("pending", lifecycle)
        try:
            with _scoped_question_request_id(request_id):
                answers = asker(normalized)
        except Exception as exc:
            _report_lifecycle("error", {**lifecycle, "error": str(exc)})
            raise
        if answers is None:
            _report_lifecycle("terminated", lifecycle)
            return ToolOutput(
                "User dismissed the question.",
                title="Question dismissed",
                metadata={"answers": [], "dismissed": True},
            )
        if not isinstance(answers, list) or len(answers) != len(normalized):
            _report_lifecycle(
                "error",
                {**lifecycle, "error": "Interactive question service returned malformed answers"},
            )
            return "Error: Interactive question service returned malformed answers"

        formatted: list[str] = []
        for item, answer in zip(normalized, answers):
            if not isinstance(answer, list) or not all(isinstance(value, str) for value in answer):
                _report_lifecycle(
                    "error",
                    {**lifecycle, "error": "Interactive question service returned malformed answers"},
                )
                return "Error: Interactive question service returned malformed answers"
            values = [value.strip() for value in answer if value.strip()]
            response = ", ".join(values) if values else "Unanswered"
            formatted.append(
                f'{json.dumps(item["question"], ensure_ascii=False)}='
                f'{json.dumps(response, ensure_ascii=False)}'
            )
        reply_answers = [list(answer) for answer in answers]
        _report_lifecycle("completed", {**lifecycle, "answers": reply_answers})
        return ToolOutput(
            "User has answered your questions: "
            + ", ".join(formatted)
            + ". You can now continue with the user's answers in mind.",
            title=(
                f"Asked {len(normalized)} question"
                f"{'s' if len(normalized) > 1 else ''}"
            ),
            metadata={"answers": reply_answers},
        )
    except Exception as exc:
        return format_public_error(exc)


register(
    name="question",
    description=(
        "Ask the user 1-4 structured questions only when a material decision cannot "
        "be resolved from the request, repository, or sensible defaults. Put a "
        "recommended choice first and suffix its label with '(Recommended)'. Users "
        "may type a custom answer, so do not add an Other option."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "header": {"type": "string"},
                        "multiple": {"type": "boolean"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["question", "header", "options"],
                },
            },
        },
        "required": ["questions"],
    },
    handler=question,
    execution="serial",
    plan_mode_allowed=True,
)
