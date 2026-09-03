"""Stable non-interactive product surface backed by the Native SDK runtime."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Callable, TextIO

from nz_coder.interface.submission import build_user_submission, resolve_submission_files
from nz_coder.foundation.json_safety import json_safe_value
from nz_coder.providers.models import active_model_selection
from nz_coder.runtime.conversation import prompt
from nz_coder.runtime.core.events import RuntimeEvent
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunResult, RunStatus
from nz_coder.sdk import AgentClient
from nz_coder.state.sessions import create_session_id, load_session
from nz_coder.state.workdir import scoped_workdir


EXIT_SUCCESS = 0
EXIT_TASK_FAILED = 1
EXIT_USAGE = 2
EXIT_PROVIDER = 3
EXIT_CANCELLED = 4


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="nz-coder run", add_help=True)
    parser.add_argument(
        "-p", "--prompt", dest="prompt_option", metavar="TEXT",
        help="task prompt (positional text and stdin are also supported)",
    )
    parser.add_argument("prompt", nargs="*", help="task prompt as positional text")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--variant", "--effort", dest="variant")
    parser.add_argument(
        "--permission-mode", default="default",
        choices=("default", "auto", "plan", "acceptEdits"),
    )
    parser.add_argument("--session")
    parser.add_argument("--continue", dest="continue_session", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--output", choices=("text", "json", "jsonl"), default="text")
    return parser


def _stdin_text(stream: TextIO) -> str:
    try:
        if stream.isatty():
            return ""
    except (AttributeError, OSError):
        pass
    return stream.read().strip()


def _session_id(args) -> str:
    selectors = sum(bool(item) for item in (
        args.session, args.continue_session, args.resume,
    ))
    if selectors > 1:
        raise ValueError("--session, --continue, and --resume are mutually exclusive")
    if args.resume:
        if not load_session(args.resume):
            raise ValueError(f"Session does not exist: {args.resume}")
        return args.resume
    if args.continue_session:
        payload = load_session("latest")
        selected = payload.get("session_id") if payload else None
        if not selected:
            raise ValueError("No previous session is available to continue")
        return str(selected)
    return args.session or create_session_id("run")


def _result_record(result: RunResult) -> dict:
    usage = result.usage
    record = {
        "session_id": result.session_id,
        "status": result.status.value,
        "text": result.final_text,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cached_read_tokens": usage.cached_read_tokens,
            "cached_write_tokens": usage.cached_write_tokens,
            "total_tokens": usage.total_tokens,
        },
        "changed_files": list(result.metadata.get("changed_files", [])),
        "error": result.error or None,
    }
    runtime = result.metadata.get("runtime")
    if isinstance(runtime, dict) and int(runtime.get("provider_calls") or 0) > 0:
        provider_record = {
            "calls": int(runtime.get("provider_calls") or 0),
            "attempts": int(runtime.get("provider_attempts") or 0),
            "calls_by_purpose": dict(
                runtime.get("provider_calls_by_purpose") or {}
            ),
            "usage_by_purpose": {
                str(purpose): dict(values)
                for purpose, values in (
                    runtime.get("provider_usage_by_purpose") or {}
                ).items()
                if isinstance(values, dict)
            },
        }
        calls_by_model = dict(runtime.get("provider_calls_by_model") or {})
        usage_by_model = {
                str(model): dict(values)
                for model, values in (
                    runtime.get("provider_usage_by_model") or {}
                ).items()
                if isinstance(values, dict)
        }
        if calls_by_model:
            provider_record["calls_by_model"] = calls_by_model
        if usage_by_model:
            provider_record["usage_by_model"] = usage_by_model
        if "provider_cost_usd" in runtime:
            provider_record.update({
                "cost_usd": float(runtime.get("provider_cost_usd") or 0.0),
                "cost_usd_by_purpose": dict(
                    runtime.get("provider_cost_usd_by_purpose") or {}
                ),
                "cost_usd_by_model": dict(
                    runtime.get("provider_cost_usd_by_model") or {}
                ),
                "cost_unknown_calls": int(
                    runtime.get("provider_cost_unknown_calls") or 0
                ),
                "cost_sources": dict(runtime.get("provider_cost_sources") or {}),
            })
        record["provider"] = provider_record
    return record


def _event_record(event: RuntimeEvent) -> dict:
    name = event.name.value if hasattr(event.name, "value") else str(event.name)
    return {
        "type": "runtime_event",
        "event": name,
        "timestamp": event.timestamp,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "agent_id": event.agent_id,
        "parent_run_id": event.parent_run_id,
        "payload": event.payload,
    }


def _provider_failure(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(word in text for word in (
        "api_key", "credential", "authentication", "unauthorized", "provider",
    ))


def _json_record(value: object) -> str:
    return json.dumps(
        json_safe_value(value),
        ensure_ascii=False,
        allow_nan=False,
    )


async def _run(args, *, stdin: TextIO, stdout: TextIO, client_factory: Callable) -> int:
    workspace = Path(args.cwd).resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")
    if args.max_turns is not None and args.max_turns < 1:
        raise ValueError("--max-turns must be a positive integer")
    option_prompt = str(args.prompt_option or "").strip()
    positional = " ".join(args.prompt).strip()
    piped = _stdin_text(stdin)
    user_text = "\n\n".join(
        part for part in (option_prompt, positional, piped) if part
    )
    if not user_text:
        raise ValueError("A prompt is required as an argument or on stdin")

    with scoped_workdir(workspace):
        from nz_coder.interface.custom_commands import default_command_catalog
        from nz_coder.foundation.workspace_trust import load_config_snapshot
        from nz_coder.state.skills import SkillLoader

        expanded_command = default_command_catalog(workspace).expand_invocation(user_text)
        workspace_snapshot = load_config_snapshot(workspace)
        skill_loader = SkillLoader(
            project_dir=workspace / ".nz-coder" / "skills",
            workspace_trusted=workspace_snapshot.control_plane_trusted,
        )
        command_tools: tuple[str, ...] = ()
        command_model: str | None = None
        if expanded_command is not None:
            user_text = expanded_command.prompt
            command_tools = expanded_command.allowed_tools
            command_model = expanded_command.model
        session_id = _session_id(args)
        selected = active_model_selection(workspace)
        provider = args.provider or selected.provider
        model = args.model or command_model or selected.model_id
        if "/" in model and args.provider is None and command_model == model:
            command_provider, command_model_id = model.split("/", 1)
            provider, model = command_provider, command_model_id
        variant = args.variant if args.variant is not None else selected.variant
        files = resolve_submission_files([*args.file, *args.attach], workspace)
        message = build_user_submission(
            user_text,
            files,
            workspace=workspace,
            session_id=session_id,
            agent="headless",
            provider_id=provider,
            model_id=model,
            variant=variant,
        )
        request = RunRequest(
            agent=AgentDefinition(
                name="headless",
                instructions=prompt.build(
                    memory_block="", skill_descriptions=skill_loader.descriptions(),
                ),
                allowed_tools=command_tools or None,
            ),
            profile=MAIN_PROFILE,
            messages=(message,),
            workspace=workspace,
            session_id=session_id,
            tool_names=command_tools,
            stream=False,
            provider=provider,
            model=model,
            reasoning_effort=variant,
            metadata={
                "permission_mode": args.permission_mode,
                "persist_session": not args.no_session,
                **({"max_turns": args.max_turns} if args.max_turns else {}),
            },
        )

        def on_event(event: RuntimeEvent) -> None:
            if args.output == "jsonl":
                stdout.write(_json_record(_event_record(event)) + "\n")
                stdout.flush()

        result = await client_factory().run(request, on_event=on_event)
    record = _result_record(result)
    if args.output == "text":
        if result.final_text:
            stdout.write(result.final_text.rstrip("\n") + "\n")
    elif args.output == "json":
        stdout.write(_json_record(record) + "\n")
    else:
        stdout.write(_json_record({"type": "result", **record}) + "\n")
    if result.status in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
        return EXIT_CANCELLED
    return EXIT_SUCCESS if result.status is RunStatus.COMPLETED else EXIT_TASK_FAILED


def run_main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    client_factory: Callable[[], AgentClient] = AgentClient,
) -> int:
    """Run a single task without terminal decoration and return a stable exit code."""
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        parser = _parser()
        if "-h" in raw_args or "--help" in raw_args:
            output_stream.write(parser.format_help())
            return EXIT_SUCCESS
        args = parser.parse_args(raw_args)
        return asyncio.run(_run(
            args, stdin=input_stream, stdout=output_stream, client_factory=client_factory,
        ))
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        error_stream.write(f"Cancelled: {exc or 'user interrupt'}\n")
        return EXIT_CANCELLED
    except BaseException as exc:
        code = EXIT_PROVIDER if _provider_failure(exc) else EXIT_USAGE
        error_stream.write(f"Error: {exc}\n")
        return code


__all__ = ["run_main"]
