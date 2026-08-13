"""Tests for the async fuzzy terminal selector."""
from __future__ import annotations

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nz_coder.interface.selector import FuzzySelector, _fuzzy_score


def test_fuzzy_score_prefers_exact_prefix_substring_and_subsequence():
    exact = _fuzzy_score("alpha", "alpha")
    prefix = _fuzzy_score("alpha", "alphabet")
    substring = _fuzzy_score("alpha", "the alpha model")
    subsequence = _fuzzy_score("apm", "alpha model")

    assert exact < prefix < substring < subsequence
    assert _fuzzy_score("xyz", "alpha model") is None


def test_selector_filters_and_resets_selection():
    selector = FuzzySelector(
        title="Models",
        values=[
            ("openai", "OpenAI GPT"),
            ("claude", "Anthropic Claude"),
            ("gemini", "Google Gemini"),
        ],
    )
    selector.move(2)

    selector.query = "acl"
    selector.selected = 0

    assert [item.value for item in selector.filtered_options()] == ["claude"]
    assert selector.current_value() == "claude"


def test_selector_wraps_navigation_and_handles_no_matches():
    selector = FuzzySelector(
        title="Sessions",
        values=[("one", "One"), ("two", "Two")],
    )

    selector.move(-1)
    assert selector.current_value() == "two"
    selector.query = "missing"
    assert selector.current_value() is None


def test_selector_renders_only_a_bounded_window_around_selection():
    selector = FuzzySelector(
        title="Sessions",
        values=[(index, f"Session {index:02d}") for index in range(30)],
    )
    selector.selected = 20

    rendered = "".join(text for _style, text in selector._render_results())

    assert rendered.count("Session ") == 14
    assert "Session 20" in rendered
    assert "Session 00" not in rendered


def test_selector_application_filters_and_accepts_with_single_enter():
    async def exercise(pipe_input) -> object | None:
        selector = FuzzySelector(
            title="Models",
            values=[("alpha", "Alpha model"), ("beta", "Beta model")],
        )
        application = selector.application(input=pipe_input, output=DummyOutput())
        task = asyncio.create_task(application.run_async())
        await asyncio.sleep(0.01)
        pipe_input.send_text("bt\r")
        return await task

    with create_pipe_input() as pipe_input:
        assert asyncio.run(exercise(pipe_input)) == "beta"


def test_selector_application_escape_cancels():
    async def exercise(pipe_input) -> object | None:
        selector = FuzzySelector(title="Models", values=[("alpha", "Alpha")])
        application = selector.application(input=pipe_input, output=DummyOutput())
        task = asyncio.create_task(application.run_async())
        await asyncio.sleep(0.01)
        pipe_input.send_text("\x1b")
        return await task

    with create_pipe_input() as pipe_input:
        assert asyncio.run(exercise(pipe_input)) is None


def test_selector_accepts_custom_answer_when_no_option_matches():
    async def exercise(pipe_input) -> object | None:
        selector = FuzzySelector(
            title="Storage",
            values=[("sqlite", "SQLite"), ("postgres", "PostgreSQL")],
            allow_custom=True,
        )
        application = selector.application(input=pipe_input, output=DummyOutput())
        task = asyncio.create_task(application.run_async())
        await asyncio.sleep(0.01)
        pipe_input.send_text("existing database\r")
        return await task

    with create_pipe_input() as pipe_input:
        assert asyncio.run(exercise(pipe_input)) == "existing database"


def test_selector_multiple_toggles_choices_and_submits_once():
    async def exercise(pipe_input) -> object | None:
        selector = FuzzySelector(
            title="Scope",
            values=[("file", "Current file"), ("repo", "Repository")],
            multiple=True,
        )
        application = selector.application(input=pipe_input, output=DummyOutput())
        task = asyncio.create_task(application.run_async())
        await asyncio.sleep(0.01)
        pipe_input.send_text(" \x1b[B \r")
        return await task

    with create_pipe_input() as pipe_input:
        assert asyncio.run(exercise(pipe_input)) == ("file", "repo")


def test_selector_multiple_accepts_custom_answer_containing_spaces():
    async def exercise(pipe_input) -> object | None:
        selector = FuzzySelector(
            title="Scope",
            values=[("file", "Current file"), ("repo", "Repository")],
            multiple=True,
            allow_custom=True,
        )
        application = selector.application(input=pipe_input, output=DummyOutput())
        task = asyncio.create_task(application.run_async())
        await asyncio.sleep(0.01)
        pipe_input.send_text("generated files\r")
        return await task

    with create_pipe_input() as pipe_input:
        assert asyncio.run(exercise(pipe_input)) == ("generated files",)
