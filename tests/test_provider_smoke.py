"""Offline tests for opt-in provider live smoke orchestration."""
from __future__ import annotations

from nz_coder.evaluation.provider_smoke import main, run_provider_smoke
from nz_coder.providers.normalized import (
    NormalizedFunction,
    NormalizedToolCall,
    chunk,
    completion,
)


class _FakeProvider:
    name = "fake"

    def __init__(self):
        self.requests = []

    def create_client(self):
        return object()

    def create_completion(self, client, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return iter([chunk(content="NZ_STREAM_SMOKE_OK")])
        if kwargs.get("tool_choice"):
            return completion(
                tool_calls=[
                    NormalizedToolCall(
                        id="call-smoke",
                        function=NormalizedFunction(
                            name="provider_smoke_echo",
                            arguments='{"value": "NZ_TOOL_SMOKE_OK"}',
                        ),
                    )
                ]
            )
        if kwargs["messages"][-1]["role"] == "tool":
            return completion(content="NZ_TOOL_SMOKE_OK")
        return completion(content="NZ_PROVIDER_SMOKE_OK")


def test_run_provider_smoke_covers_text_tool_round_trip_and_stream():
    provider = _FakeProvider()
    report = run_provider_smoke(provider, model="fake-model")

    assert report.ok
    assert report.provider == "fake"
    assert [check.name for check in report.checks] == [
        "text",
        "tool",
        "stream",
    ]
    assert len(provider.requests) == 4
    assert provider.requests[1]["tool_choice"]["function"]["name"] == (
        "provider_smoke_echo"
    )
    assert provider.requests[2]["messages"][-1]["role"] == "tool"
    assert "tools" not in provider.requests[2]
    assert provider.requests[3]["stream"] is True


def test_run_provider_smoke_reports_check_failure_without_raising():
    class EmptyProvider(_FakeProvider):
        def create_completion(self, client, **kwargs):
            return completion(content="")

    report = run_provider_smoke(
        EmptyProvider(),
        model="fake-model",
        checks=("text",),
    )

    assert not report.ok
    assert report.checks[0].ok is False
    assert "empty text" in report.checks[0].detail


def test_tool_smoke_retries_without_unsupported_forced_choice():
    class FallbackProvider(_FakeProvider):
        def create_completion(self, client, **kwargs):
            self.requests.append(kwargs)
            if kwargs.get("tool_choice"):
                raise RuntimeError("thinking mode does not support this tool_choice")
            if kwargs["messages"][-1]["role"] == "tool":
                return completion(content="NZ_TOOL_SMOKE_OK")
            return completion(
                tool_calls=[
                    NormalizedToolCall(
                        id="call-fallback",
                        function=NormalizedFunction(
                            name="provider_smoke_echo",
                            arguments='{"value": "NZ_TOOL_SMOKE_OK"}',
                        ),
                    )
                ]
            )

    provider = FallbackProvider()
    report = run_provider_smoke(
        provider,
        model="thinking-model",
        checks=("tool",),
    )

    assert report.ok
    assert report.checks[0].detail.endswith("forced_tool_choice=False")
    assert len(provider.requests) == 3
    assert "tool_choice" in provider.requests[0]
    assert "tool_choice" not in provider.requests[1]


def test_tool_smoke_requests_a_final_answer_after_the_tool_result():
    class InstructionFollowingProvider(_FakeProvider):
        def create_completion(self, client, **kwargs):
            self.requests.append(kwargs)
            if kwargs["messages"][-1]["role"] == "tool":
                initial_request = kwargs["messages"][1]["content"]
                if "Do not answer directly" in initial_request:
                    return completion(content="", reasoning_content="Tool call completed.")
                return completion(content="NZ_TOOL_SMOKE_OK")
            return completion(
                tool_calls=[
                    NormalizedToolCall(
                        id="call-instruction-following",
                        function=NormalizedFunction(
                            name="provider_smoke_echo",
                            arguments='{"value": "NZ_TOOL_SMOKE_OK"}',
                        ),
                    )
                ]
            )

    report = run_provider_smoke(
        InstructionFollowingProvider(),
        model="thinking-model",
        checks=("tool",),
    )

    assert report.ok


def test_provider_smoke_cli_defaults_to_safe_dry_run(capsys):
    exit_code = main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Dry run only" in output
    assert "no API request sent" in output
