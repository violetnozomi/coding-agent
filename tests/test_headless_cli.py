"""Product contracts for the Native headless command."""
from __future__ import annotations

import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest

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
        self.run_kwargs = []
        self.run_snapshots = []

    async def run(self, request, **kwargs):
        from nz_coder.foundation.workspace_trust import active_config_snapshot

        self.requests.append(request)
        self.run_kwargs.append(dict(kwargs))
        self.run_snapshots.append(active_config_snapshot(request.workspace))
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
        "verification": {},
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


def test_headless_expands_project_command_and_narrows_tools(tmp_path, monkeypatch):
    commands = tmp_path / ".nz-coder" / "commands"
    commands.mkdir(parents=True)
    (commands / "review.md").write_text(
        "---\ndescription: Review scope\nallowed_tools:\n  - read_file\n  - grep_search\n"
        "---\nReview only: $ARGUMENTS",
        encoding="utf-8",
    )
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )
    trust_path = tmp_path.parent / f"{tmp_path.name}-command-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-control", snapshot.control_fingerprint
    )

    code, _stdout, stderr, client = _run(["/review", "src/runtime"], tmp_path)

    request = client.requests[0]
    assert code == 0
    assert stderr == ""
    assert request.messages[0]["content"].endswith("Review only: src/runtime")
    assert request.tool_names == ("read_file", "grep_search")
    assert request.agent.allowed_tools == ("read_file", "grep_search")


def test_headless_project_skill_uses_submission_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    skill = tmp_path / ".nz-coder" / "skills" / "repo-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: repo-review\ndescription: Target workspace marker\n"
        "---\nTARGET_WORKSPACE_SKILL_MARKER\n",
        encoding="utf-8",
    )
    trust_path = tmp_path.parent / f"{tmp_path.name}-headless-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))

    _code, _stdout, _stderr, untrusted_client = _run(["inspect"], tmp_path)
    assert "TARGET_WORKSPACE_SKILL_MARKER" not in (
        untrusted_client.requests[0].agent.instructions
    )

    snapshot = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-control", snapshot.control_fingerprint
    )
    _code, _stdout, _stderr, trusted_client = _run(["inspect"], tmp_path)
    assert "repo-review" not in trusted_client.requests[0].agent.instructions
    run_snapshot = trusted_client.run_snapshots[0]
    assert run_snapshot.control_plane_trusted is True
    assert run_snapshot.project_control.get(
        ".nz-coder/skills/repo-review/SKILL.md"
    ) is not None


def test_headless_skill_descriptions_are_injected_once(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    skill = tmp_path / ".nz-coder" / "skills" / "once" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: once\ndescription: UNIQUE_SKILL_DESCRIPTION\n---\nbody\n",
        encoding="utf-8",
    )
    trust_path = tmp_path.parent / f"{tmp_path.name}-skill-once-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    initial = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-control", initial.control_fingerprint,
    )

    code, _stdout, _stderr, client = _run(["inspect"], tmp_path)

    assert code == 0
    instructions = client.requests[0].agent.instructions
    assert instructions.count("## Available skills") == 0
    assert instructions.count("UNIQUE_SKILL_DESCRIPTION") == 0

    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    snapshot = client.run_snapshots[0]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            instructions,
            client=type("Client", (), {})(),
            config_snapshot=snapshot,
            trace_enabled=False,
            sidecar_verifier=False,
        )
        bundle = agent.prepare_run_control(snapshot)
        try:
            assert agent.system_prompt.count("## Available skills") == 1
            assert agent.system_prompt.count("UNIQUE_SKILL_DESCRIPTION") == 1
        finally:
            agent.retire_run_control(bundle)
            agent.close()


def test_headless_skill_change_does_not_affect_inflight_run(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    skill = tmp_path / ".nz-coder" / "skills" / "epoch" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: epoch\ndescription: SKILL_EPOCH_ONE\n---\nbody\n",
        encoding="utf-8",
    )
    trust_path = tmp_path.parent / f"{tmp_path.name}-skill-epoch-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    pending = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-control", pending.control_fingerprint,
    )
    submission_snapshot = load_config_snapshot(tmp_path)
    skill.write_text(
        "---\nname: epoch\ndescription: SKILL_EPOCH_TWO\n---\nbody\n",
        encoding="utf-8",
    )

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "base", client=type("Client", (), {})(),
            config_snapshot=submission_snapshot,
            trace_enabled=False, sidecar_verifier=False,
        )
        bundle = agent.prepare_run_control(submission_snapshot)
        try:
            assert agent.system_prompt.count("SKILL_EPOCH_ONE") == 1
            assert "SKILL_EPOCH_TWO" not in agent.system_prompt
        finally:
            agent.retire_run_control(bundle)
            agent.close()


def test_headless_next_run_rotates_skill_description(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    skill = tmp_path / ".nz-coder" / "skills" / "rotate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    trust_path = tmp_path.parent / f"{tmp_path.name}-skill-rotate-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    store = WorkspaceTrustStore(trust_path)

    skill.write_text(
        "---\nname: rotate\ndescription: ROTATING_SKILL_ONE\n---\nbody\n",
        encoding="utf-8",
    )
    pending = load_config_snapshot(tmp_path)
    store.trust(tmp_path, "workspace-control", pending.control_fingerprint)
    first = load_config_snapshot(tmp_path)

    skill.write_text(
        "---\nname: rotate\ndescription: ROTATING_SKILL_TWO\n---\nbody\n",
        encoding="utf-8",
    )
    pending = load_config_snapshot(tmp_path)
    store.trust(tmp_path, "workspace-control", pending.control_fingerprint)
    second = load_config_snapshot(tmp_path)

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "base", client=type("Client", (), {})(), config_snapshot=first,
            trace_enabled=False, sidecar_verifier=False,
        )
        first_bundle = agent.prepare_run_control(first)
        assert "ROTATING_SKILL_ONE" in agent.system_prompt
        agent.retire_run_control(first_bundle)
        second_bundle = agent.prepare_run_control(second)
        try:
            assert "ROTATING_SKILL_ONE" not in agent.system_prompt
            assert agent.system_prompt.count("ROTATING_SKILL_TWO") == 1
            assert agent.system_prompt.count("## Available skills") == 1
        finally:
            agent.retire_run_control(second_bundle)
            agent.close()


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
    assert code == 1
    assert "native headless complete" in stdout.getvalue()
    assert "未经独立确认" in stdout.getvalue()
    assert "未评估" in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert provider.calls == 2


@pytest.mark.parametrize("persist_session", [False, True])
def test_headless_native_write_respects_session_persistence(
    monkeypatch, tmp_path, persist_session,
):
    from nz_coder.interface.headless import run_main
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions

    with scoped_workdir(tmp_path):
        sessions.save_session(
            [{"role": "user", "content": "previous durable session"}],
            session_id="previous", run_status="completed",
        )
        state = sessions.session_dir()
    aliases = {name: (state / name).read_bytes() for name in ("latest.json", "active.json")}

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            tool_calls = [] if self.calls > 1 else [SimpleNamespace(
                id="call-write", type="function", function=SimpleNamespace(
                    name="write_file",
                    arguments='{"path":"headless.txt","content":"HEADLESS_WRITE_OK\\n"}',
                ),
            )]
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content="" if tool_calls else "HEADLESS_WRITE_OK",
                        tool_calls=tool_calls,
                    ),
                    finish_reason="tool_calls" if tool_calls else "stop",
                )],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            )

    runtime = ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model", request_model_id="offline-model",
        variant=None, provider=Provider(), client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
    )
    monkeypatch.setattr(
        "nz_coder.runtime.execution.native_sdk.resolve_model_runtime", lambda _: runtime,
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["--cwd", str(tmp_path), "--provider", "offline", "--model", "offline-model",
            "--permission-mode", "acceptEdits", "--output", "json", "--max-turns", "4"]
    if not persist_session:
        argv.append("--no-session")
    code = run_main(
        [*argv, "Create headless.txt containing HEADLESS_WRITE_OK followed by a newline."],
        stdin=io.StringIO(), stdout=stdout, stderr=stderr,
    )
    result = json.loads(stdout.getvalue())
    assert code == 1, stderr.getvalue()
    assert result["status"] == "completed_unverified"
    assert (tmp_path / "headless.txt").read_bytes() == b"HEADLESS_WRITE_OK\n"
    path = state / f'{result["session_id"]}.json'
    if persist_session:
        saved = json.loads(path.read_text())
        assert saved["run_status"] == "completed_unverified"
        assert [message["role"] for message in saved["messages"]] == [
            "user", "assistant", "tool", "assistant",
        ]
        assert json.loads((state / "latest.json").read_text())["session_id"] == result["session_id"]
        assert json.loads((state / "active.json").read_text())["session_id"] == result["session_id"]
    else:
        assert not path.exists()
        assert {name: (state / name).read_bytes() for name in aliases} == aliases


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

    assert code == 1
    assert provider.requests
    user = next(item for item in provider.requests[0]["messages"] if item["role"] == "user")
    assert user["_nz_user_attachments"][0]["mime"] == "image/png"


@pytest.mark.parametrize('output', ['json', 'jsonl'])
def test_unverified_headless_preserves_snapshot_and_returns_failure(tmp_path, output):
    verification = {'verification_needed': True, 'ignored_verifications': [{'command': 'pytest | tail', 'reason': 'masked exit'}]}
    result = RunResult(status=RunStatus.COMPLETED_UNVERIFIED, final_text='unchecked',
        messages=[], usage=TokenUsage(), session_id='s-unverified', active_agent='main',
        metadata={'verification': verification})
    code, stdout, stderr, _ = _run(['check', '--output', output], tmp_path, client=FakeClient(result=result))
    record = json.loads(stdout.splitlines()[-1])
    assert code == 1
    assert record['status'] == 'completed_unverified'
    assert record['verification'] == verification


@pytest.mark.parametrize("declared, passes, output, answers", [
    pytest.param(True, False, "json", "y\n", id="required-test-fails"),
    pytest.param(False, False, "jsonl", "invalid\ny\n", id="observed-test-fails"),
    pytest.param(True, True, "json", "p\n", id="test-passes-review-pending"),
])
def test_headless_reports_actual_test_result_and_persists_same_evidence(
    monkeypatch, tmp_path, capsys, declared, passes, output, answers,
):
    from nz_coder.interface.headless import run_main
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import load_session
    from tests.runtime.test_native_product_runtime import _install_runtime
    from tests.runtime.test_working_memory_reminders import _call, _reply

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path / "config.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))

    class TerminalInput(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))
    command = "python -m unittest test_value"
    (workspace / "test_value.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        f" def test_value(self): self.assertEqual(1, {1 if passes else 2})\n"
    )

    class Provider:
        name = "offline"
        check_requested = False

        def create_completion(self, _client, **kwargs):
            if not self.check_requested:
                self.check_requested = True
                return _reply(kwargs, calls=[_call(1, "bash", {"command": command})])
            return _reply(kwargs, text="Finished running the test.")

    _install_runtime(monkeypatch, Provider())
    prompt = "Run the tests and report whether they pass."
    if declared:
        prompt += f" Acceptance command: `{command}`."
    code = run_main(
        ["--cwd", str(workspace), "--provider", "offline", "--model", "offline-model",
         "--max-turns", "5", "--output", output, prompt],
    )
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    if output == "json":
        assert len(records) == 1
    else:
        assert records[-1]["type"] == "result"
    assert command in captured.err and "Allow?" in captured.err
    if not declared:
        assert "Please answer y/n/a/p" in captured.err
    record = records[-1]
    with scoped_workdir(workspace):
        saved = load_session(record["session_id"])
    snapshot = record["verification"]
    assert saved["metadata"]["verification"] == snapshot
    assert saved["run_status"] == record["status"]
    assert saved["messages"][-1]["content"] == record["text"]
    receipt = next(m for m in saved["messages"] if m.get("tool_call_id") == "memory-1")
    assert "Ran 1 test" in receipt["content"]
    assert receipt["_nz_verification_passed"] is passes
    assert snapshot["last_verification"]["status"] == ("passed" if passes else "failed")
    assert snapshot["verification_needed"] is (declared and not passes)
    assert snapshot["tests_passed"] is (passes if declared else None)
    assert code == 1 and not snapshot["completion_ready"]
    if declared and not passes:
        assert record["status"] == "blocked"
        assert snapshot["acceptance_checks"][0]["status"] == "failed"
        assert command in record["text"] and "FAILED (failures=1)" in record["text"]
    else:
        assert record["status"] == "completed_unverified"
        assert snapshot["review_pending"] and "交付评审尚未确认" in record["text"]
    if not declared:
        assert snapshot["test_obligations"] == []
        assert snapshot["ordinary_failures"][0]["exit_code"] == 1
        assert "退出码 1" in record["text"]
        assert "没有已采信的验证执行" not in record["text"]


def test_headless_resume_reports_corrupt_identity_before_client(tmp_path):
    from nz_coder.state import sessions
    from nz_coder.state.workdir import scoped_workdir
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'keep'}], session_id='resume-bad', activate=False, model='test')
        payload = json.loads(path.read_text())
        payload['session_id'] = 'other'
        path.write_text(json.dumps(payload))
        original = path.read_bytes()
    code, stdout, stderr, client = _run(['--resume', 'resume-bad', 'continue'], tmp_path)
    assert code != 0 and stdout == ''
    assert 'corrupt' in stderr and str(path) not in stderr
    assert not client.requests
    assert path.read_bytes() == original
