"""Product contracts for the Native headless command."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.runtime.core.events import RuntimeEvent, RuntimeEventName
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage


class FakeClient:
    def __init__(
        self,
        result: RunResult | None = None,
        error: Exception | None = None,
        event_payload: dict | None = None,
    ):
        self.result = result
        self.error = error
        self.event_payload = event_payload or {}
        self.requests = []

    async def run(self, request, **kwargs):
        self.requests.append(request)
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(RuntimeEvent(
                name=RuntimeEventName.RUN_STARTED,
                run_id=request.session_id,
                session_id=request.session_id,
                payload=self.event_payload,
            ))
        if self.error is not None:
            raise self.error
        return self.result or RunResult(
            status=RunStatus.COMPLETED,
            final_text="done",
            messages=request.messages,
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            session_id=request.session_id,
            active_agent=request.agent.name,
            metadata={"changed_files": ["src/app.py"]},
        )


def _run(argv, tmp_path, *, stdin_text="", client=None):
    from nz_coder.interface.headless import run_main

    stdout = io.StringIO()
    stderr = io.StringIO()
    selected = client or FakeClient()
    code = run_main(
        ["--cwd", str(tmp_path), *argv],
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda: selected,
    )
    return code, stdout.getvalue(), stderr.getvalue(), selected


def test_headless_text_combines_positional_prompt_and_piped_stdin(tmp_path):
    code, stdout, stderr, client = _run(
        ["inspect", "repository"], tmp_path, stdin_text="extra constraints",
    )

    assert code == 0
    assert stdout == "done\n"
    assert stderr == ""
    assert client.requests[0].messages[0]["content"].endswith(
        "inspect repository\n\nextra constraints"
    )


def test_headless_accepts_documented_prompt_option(tmp_path):
    code, stdout, stderr, client = _run(
        ["--prompt", "inspect repository"], tmp_path,
    )

    assert code == 0
    assert stdout == "done\n"
    assert stderr == ""
    assert client.requests[0].messages[0]["content"].endswith(
        "inspect repository"
    )


def test_headless_json_is_one_clean_machine_record(tmp_path):
    code, stdout, stderr, _client = _run(
        ["--output", "json", "summarize"], tmp_path,
    )

    payload = json.loads(stdout)
    assert code == 0
    assert stderr == ""
    assert "\x1b" not in stdout
    assert payload == {
        "session_id": payload["session_id"],
        "status": "completed",
        "text": "done",
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_read_tokens": 0,
            "cached_write_tokens": 0,
            "total_tokens": 5,
        },
        "changed_files": ["src/app.py"],
        "error": None,
    }


def test_headless_json_exposes_provider_call_breakdown_when_available(tmp_path):
    result = RunResult(
        status=RunStatus.COMPLETED,
        final_text="done",
        messages=(),
        usage=TokenUsage(input_tokens=13, output_tokens=3),
        session_id="provider-accounting",
        active_agent="headless",
        metadata={
            "runtime": {
                "provider_calls": 2,
                "provider_attempts": 3,
                "provider_calls_by_purpose": {"planning": 1, "coding": 1},
                "provider_calls_by_model": {
                    "openai-responses/gpt-planner": 1,
                    "anthropic/claude-coder": 1,
                },
                "provider_usage_by_purpose": {
                    "planning": {"input": 3, "output": 1, "total": 4},
                    "coding": {"input": 10, "output": 2, "total": 12},
                },
                "provider_usage_by_model": {
                    "openai-responses/gpt-planner": {
                        "input": 3, "output": 1, "total": 4,
                    },
                    "anthropic/claude-coder": {
                        "input": 10, "output": 2, "total": 12,
                    },
                },
                "provider_cost_usd": 0.125,
                "provider_cost_usd_by_purpose": {
                    "planning": 0.025,
                    "coding": 0.1,
                },
                "provider_cost_usd_by_model": {
                    "openai-responses/gpt-planner": 0.025,
                    "anthropic/claude-coder": 0.1,
                },
                "provider_cost_unknown_calls": 0,
                "provider_cost_sources": {"provider": 1, "registry": 1},
            },
        },
    )

    code, stdout, stderr, _client = _run(
        ["--output", "json", "summarize"],
        tmp_path,
        client=FakeClient(result=result),
    )

    payload = json.loads(stdout)
    assert code == 0
    assert stderr == ""
    assert payload["provider"] == {
        "calls": 2,
        "attempts": 3,
        "calls_by_purpose": {"planning": 1, "coding": 1},
        "calls_by_model": result.metadata["runtime"]["provider_calls_by_model"],
        "usage_by_purpose": result.metadata["runtime"]["provider_usage_by_purpose"],
        "usage_by_model": result.metadata["runtime"]["provider_usage_by_model"],
        "cost_usd": 0.125,
        "cost_usd_by_purpose": {
            "planning": 0.025,
            "coding": 0.1,
        },
        "cost_usd_by_model": {
            "openai-responses/gpt-planner": 0.025,
            "anthropic/claude-coder": 0.1,
        },
        "cost_unknown_calls": 0,
        "cost_sources": {"provider": 1, "registry": 1},
    }


def test_headless_jsonl_projects_runtime_events_then_result(tmp_path):
    code, stdout, stderr, _client = _run(
        ["--output", "jsonl", "fix"], tmp_path,
    )

    records = [json.loads(line) for line in stdout.splitlines()]
    assert code == 0
    assert stderr == ""
    assert records[0]["type"] == "runtime_event"
    assert records[0]["event"] == "session.run.started"
    assert records[-1]["type"] == "result"
    assert records[-1]["status"] == "completed"


def test_headless_jsonl_repairs_nonfinite_extension_event_payload(tmp_path):
    client = FakeClient(event_payload={"latency": float("nan")})

    code, stdout, stderr, _client = _run(
        ["--output", "jsonl", "fix"],
        tmp_path,
        client=client,
    )

    records = [json.loads(
        line,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    ) for line in stdout.splitlines()]
    assert code == 0
    assert stderr == ""
    assert records[0]["payload"]["latency"] is None


def test_headless_flags_map_to_native_run_request(tmp_path):
    code, _stdout, _stderr, client = _run([
        "--provider", "offline",
        "--model", "model-x",
        "--effort", "high",
        "--permission-mode", "plan",
        "--session", "chosen-session",
        "--max-turns", "7",
        "--no-session",
        "work",
    ], tmp_path)

    request = client.requests[0]
    assert code == 0
    assert request.provider == "offline"
    assert request.model == "model-x"
    assert request.reasoning_effort == "high"
    assert request.session_id == "chosen-session"
    assert request.metadata["permission_mode"] == "plan"
    assert request.metadata["max_turns"] == 7
    assert request.metadata["persist_session"] is False


def test_headless_auto_permission_mode_does_not_enable_classifier(tmp_path):
    """Headless keeps legacy auto semantics with zero interactive classifier."""
    code, _stdout, _stderr, client = _run([
        "--permission-mode", "auto",
        "inspect",
    ], tmp_path)

    request = client.requests[0]
    assert code == 0
    assert request.metadata["permission_mode"] == "auto"
    assert not request.metadata.get("auto_mode_classifier_enabled", False)


def test_headless_file_and_attach_share_filepart_pipeline(tmp_path):
    (tmp_path / "notes.txt").write_text("evidence", encoding="utf-8")
    (tmp_path / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\nsmall")

    code, _stdout, _stderr, client = _run([
        "--file", "notes.txt",
        "--attach", "screen.png",
        "review",
    ], tmp_path)

    message = client.requests[0].messages[0]
    assert code == 0
    assert [item["kind"] for item in message["_nz_input_expansions"]] == [
        "file", "image",
    ]
    assert any(part["type"] == "file" for part in message["_nz_parts"])


def test_headless_expands_project_command_and_narrows_tools(tmp_path):
    commands = tmp_path / ".nz-coder" / "commands"
    commands.mkdir(parents=True)
    (commands / "review.md").write_text(
        "---\ndescription: Review scope\nallowed_tools:\n  - read_file\n  - grep_search\n"
        "---\nReview only: $ARGUMENTS",
        encoding="utf-8",
    )

    code, _stdout, stderr, client = _run(["/review", "src/runtime"], tmp_path)

    request = client.requests[0]
    assert code == 0
    assert stderr == ""
    assert request.messages[0]["content"].endswith("Review only: src/runtime")
    assert request.tool_names == ("read_file", "grep_search")
    assert request.agent.allowed_tools == ("read_file", "grep_search")


def test_headless_rejects_conflicting_sessions_and_empty_input(tmp_path):
    code, stdout, stderr, client = _run([
        "--continue", "--resume", "session-1", "work",
    ], tmp_path)
    assert (code, stdout, len(client.requests)) == (2, "", 0)
    assert "mutually exclusive" in stderr

    code, stdout, stderr, client = _run([], tmp_path)
    assert (code, stdout, len(client.requests)) == (2, "", 0)
    assert "prompt" in stderr.lower()


def test_headless_exit_codes_distinguish_provider_cancel_and_task_failure(tmp_path):
    cases = [
        (RuntimeError("API_KEY credential missing"), 3),
        (asyncio.CancelledError(), 4),
    ]
    for error, expected in cases:
        client = FakeClient(error=error)
        code, stdout, stderr, _ = _run(["work"], tmp_path, client=client)
        assert code == expected
        assert stdout == ""
        assert stderr

    failed = FakeClient(result=RunResult(
        status=RunStatus.ERROR,
        final_text="",
        messages=(),
        usage=TokenUsage(),
        session_id="failed-session",
        active_agent="headless",
        error="task failed",
    ))
    code, _stdout, _stderr, _ = _run(["work"], tmp_path, client=failed)
    assert code == 1


def test_headless_resume_requires_existing_session(tmp_path):
    code, stdout, stderr, client = _run([
        "--resume", "missing-session", "continue",
    ], tmp_path)

    assert (code, stdout, len(client.requests)) == (2, "", 0)
    assert "missing-session" in stderr


def test_top_level_cli_dispatches_run_without_starting_interactive(monkeypatch):
    from nz_coder.interface import cli

    captured = []
    monkeypatch.setattr(
        "nz_coder.interface.headless.run_main",
        lambda args: captured.append(args) or 0,
    )

    assert cli.main(["run", "inspect"]) == 0
    assert captured == [["inspect"]]


def test_headless_help_uses_injected_clean_stdout(tmp_path):
    code, stdout, stderr, client = _run(["--help"], tmp_path)
    assert code == 0
    assert stdout.startswith("usage: nz-coder run")
    assert "--prompt TEXT" in stdout
    assert stderr == ""
    assert client.requests == []


def test_headless_native_path_completes_model_tool_model_without_agentloop(
    monkeypatch, tmp_path,
):
    from nz_coder.interface.headless import run_main

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="call-list", type="function",
                        function=SimpleNamespace(
                            name="list_directory",
                            arguments='{"path": ".", "depth": 1}',
                        ),
                    )],
                )
                finish = "tool_calls"
            else:
                message = SimpleNamespace(content="native headless complete", tool_calls=[])
                finish = "stop"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish)],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            )

    provider = Provider()
    runtime = ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None,
        provider=provider, client=object(),
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
        owns_client=False,
    )
    monkeypatch.setattr(
        "nz_coder.runtime.execution.native_sdk.resolve_model_runtime", lambda _request: runtime,
    )
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.AgentLoop.__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("headless must not construct AgentLoop")
        ),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_main(
        ["--cwd", str(tmp_path), "--provider", "offline", "--model", "offline-model",
         "--permission-mode", "auto", "--no-session", "inspect"],
        stdin=io.StringIO(), stdout=stdout, stderr=stderr,
    )
    assert code == 0
    assert stdout.getvalue() == "native headless complete\n"
    assert stderr.getvalue() == ""
    assert provider.calls == 2


def test_headless_native_image_reaches_vision_provider(monkeypatch, tmp_path):
    import base64

    from nz_coder.interface.headless import run_main

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
    )
    (tmp_path / "screen.png").write_bytes(image)

    class Provider:
        name = "offline"

        def __init__(self):
            self.requests = []

        def create_completion(self, _client, **kwargs):
            self.requests.append(kwargs)
            message = SimpleNamespace(content="image received", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            )

    provider = Provider()
    runtime = ResolvedModelRuntime(
        provider_id="offline", model_id="vision-model",
        request_model_id="vision-model", variant=None,
        provider=provider, client=object(),
        capabilities=ModelCapabilities(
            provider="offline", model_id="vision-model",
            supports_streaming=False, supports_image_input=True,
        ),
        owns_client=False,
    )
    monkeypatch.setattr(
        "nz_coder.runtime.execution.native_sdk.resolve_model_runtime", lambda _request: runtime,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_main(
        ["--cwd", str(tmp_path), "--provider", "offline", "--model", "vision-model",
         "--permission-mode", "auto", "--no-session", "--attach", "screen.png",
         "inspect"],
        stdin=io.StringIO(), stdout=stdout, stderr=stderr,
    )

    assert code == 0
    assert provider.requests
    user = next(item for item in provider.requests[0]["messages"] if item["role"] == "user")
    assert user["_nz_user_attachments"][0]["mime"] == "image/png"
