"""Subagent: spawn a child agent with fresh context for isolated exploration."""

import concurrent.futures
import json
import signal
import threading
import time
import uuid
from pathlib import Path

from openai import OpenAI

from nz_coder import config
from nz_coder.context import persist_large_output
from nz_coder.tools import dispatch, get_specs, register

# Subagent tool tiers.  Specs are pulled from the shared registry so high-value
# repo intelligence tools stay consistent with the parent agent.
_SUB_READ_TOOLS = {
    "bash",
    "read_file",
    "list_directory",
    "grep_search",
    "smart_search",
    "read_symbol",
    "find_symbol_callers",
    "diff_status",
    "project_profile",
    "plan_verification",
    "analyze_impact",
}
_SUB_GENERAL_EXTRA_TOOLS = {"write_file", "edit_file", "verify_changed_files"}


class SubagentTimeout(Exception):
    """Raised when a subagent API call exceeds its local budget."""


def _timeout_message(reason: str) -> str:
    return (
        f"Subagent stopped: {reason}. Continue in the main agent with direct "
        "grep_search/read_file calls and a smaller search scope."
    )


def _completion_with_timeout(client, *, timeout_seconds: int, **kwargs):
    if timeout_seconds <= 0:
        return client.chat.completions.create(**kwargs)

    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        def _handle_timeout(signum, frame):  # noqa: ARG001
            raise SubagentTimeout(f"subagent API call timed out after {timeout_seconds}s")

        old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
        old_alarm = signal.alarm(timeout_seconds)
        try:
            return client.chat.completions.create(**kwargs)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_alarm:
                signal.alarm(old_alarm)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(client.chat.completions.create, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise SubagentTimeout(f"subagent API call timed out after {timeout_seconds}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _ensure_subagent_tool_registry() -> None:
    import nz_coder.tools.bash  # noqa: F401
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.tools.repo_intel  # noqa: F401
    import nz_coder.project_profile  # noqa: F401
    import nz_coder.verification_planner  # noqa: F401
    import nz_coder.impact_analyzer  # noqa: F401
    import nz_coder.tools.search  # noqa: F401



def _subagent_tools(agent_type: str) -> list[dict]:
    _ensure_subagent_tool_registry()
    allowed = set(_SUB_READ_TOOLS)
    if agent_type == "general":
        allowed.update(_SUB_GENERAL_EXTRA_TOOLS)
    return [spec for spec in get_specs() if spec["function"]["name"] in allowed]


def _parent_context_block() -> str:
    parts: list[str] = []

    state_path = config.WORKDIR / ".nz-coder" / "runtime_state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            lines = []
            for key in (
                "turn_count", "has_diff", "diff_chars", "changed_files",
                "acceptance_criteria", "verification_attempts", "py_compile_ok",
                "broad_test_attempts", "env_noise_seen", "transition",
            ):
                value = data.get(key)
                if value not in (None, "", [], {}):
                    lines.append(f"- {key}: {value}")
            if lines:
                parts.append("Parent RuntimeState:\n" + "\n".join(lines[:12]))

    try:
        from nz_coder.tools.scratchpad import scratchpad
        scratch = scratchpad.read()
    except Exception:
        scratch = ""
    if scratch and scratch != "Scratchpad is empty.":
        parts.append("Parent scratchpad:\n" + scratch[:2000])

    if not parts:
        return ""
    return (
        "\n\nParent agent context (may be incomplete or stale; verify before acting):\n"
        + "\n\n".join(parts)
    )


def _run_allowed_tool(name: str, args: dict, agent_type: str, txn, scratch_rel: str) -> str:
    from nz_coder.tools.bash import run_bash
    from nz_coder.tools.files import edit_file, write_file

    allowed = set(_SUB_READ_TOOLS)
    if agent_type == "general":
        allowed.update(_SUB_GENERAL_EXTRA_TOOLS)
    if name not in allowed:
        return f"Error: tool not available to subagent: {name}"

    if name == "bash":
        return run_bash(args.get("command", ""), read_only=(agent_type in {"explore", "review"}))
    if name == "write_file":
        path = args.get("path", "")
        txn.track(path)
        return write_file(path, args.get("content", ""))
    if name == "edit_file":
        path = args.get("path", "")
        txn.track(path)
        return edit_file(path, args.get("old_text", ""), args.get("new_text", ""))
    return dispatch(name, args)


def _finalize_subagent_result(summary: str, scratch_path: Path, scratch_rel: str, status: str, verification: str = "") -> str:
    summary = summary or "(no summary)"
    if scratch_path.exists() and scratch_path.stat().st_size > 0 and scratch_rel not in summary:
        summary += f"\n\n[Detailed findings saved to: {scratch_rel}]"
    summary += f"\n\n[Subagent status: {status}]"
    if verification:
        summary += f"\n[Subagent verification: {verification}]"
    return summary


def _verification_passed(output: str) -> bool:
    return output.startswith(("OK:", "WARN:")) or output.startswith("No changed Python files")


def _verification_summary(output: str, max_chars: int = 1200) -> str:
    output = output.strip()
    if len(output) <= max_chars:
        return output
    return output[:max_chars] + "\n... [verification output truncated]"


def run_subagent(prompt: str, agent_type: str = "explore") -> str:
    """Spawn a subagent with fresh context. Returns only the final summary.

    The subagent is given a shared scratchpad file path. When it has detailed
    findings that exceed a natural summary, it writes them there so the parent
    agent can read the file directly rather than relying on a truncated summary.
    """
    # Ensure registry-backed tools are available even when subagent is imported
    # without going through AgentLoop's side-effect imports.
    _ensure_subagent_tool_registry()
    from nz_coder.transaction import TransactionManager

    agent_type = agent_type or "explore"

    # Provision a per-invocation scratchpad file in .nz-coder/subagent-scratch/
    scratch_dir = config.WORKDIR / ".nz-coder" / "subagent-scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch_path = scratch_dir / f"scratch-{uuid.uuid4().hex[:8]}.md"
    scratch_rel = str(scratch_path.relative_to(config.WORKDIR))

    client = OpenAI(api_key=config.API_KEY, base_url=config.API_BASE_URL)
    tools = _subagent_tools(agent_type)
    txn = TransactionManager()

    max_turns = max(1, config.SUBAGENT_MAX_TURNS)
    deadline = time.monotonic() + max(1, config.SUBAGENT_TIMEOUT_SECONDS)
    parent_context = _parent_context_block()
    system = (
        f"You are an isolated coding subagent at {config.WORKDIR}. Complete the given "
        f"task within {max_turns} turns and summarize concrete findings.\n\n"
        "Operational rules:\n"
        "- Use the current prompt plus the Parent agent context below. If paths are missing, "
        "start with smart_search, read_symbol, grep_search, or list_directory.\n"
        "- Treat parent context as a useful hint, not proof. Verify files before acting.\n"
        "- State completion criteria in your final summary: what exact evidence or file state "
        "proves the task done.\n"
        "- Prefer smart_search/read_symbol before broad grep. Do not run broad or long "
        "verification commands.\n"
        "- Modes: explore/review are read-only; test may run verification commands but not edit; general may edit.\n"
        "- In general mode, keep edits scoped to the requested task.\n\n"
        f"Shared scratchpad: {scratch_rel}\n"
        "If your findings are detailed (e.g. a list of files, discovered patterns, "
        "multi-step analysis), write them to the scratchpad file using write_file. "
        "Your final text summary should then reference the scratchpad path so the "
        "parent agent knows to read it for full details."
        f"{parent_context}"
    )
    messages = [{"role": "user", "content": prompt}]

    if agent_type == "general":
        txn.begin()

    had_write = False
    all_succeeded = True

    for _ in range(max_turns):
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            if had_write:
                rollback = txn.rollback()
                suffix = f"\n{rollback}" if rollback else ""
                return _timeout_message(f"total budget {config.SUBAGENT_TIMEOUT_SECONDS}s exceeded") + suffix
            return _timeout_message(f"total budget {config.SUBAGENT_TIMEOUT_SECONDS}s exceeded")
        try:
            resp = _completion_with_timeout(
                client,
                timeout_seconds=remaining,
                model=config.MODEL_ID,
                messages=[{"role": "system", "content": system}] + messages,
                tools=tools,
                max_tokens=8000,
            )
        except SubagentTimeout as e:
            if had_write:
                txn.rollback()
            return _timeout_message(str(e))
        except Exception as e:
            if had_write:
                txn.rollback()
            return f"Subagent error: {e}"

        choice = resp.choices[0]
        msg = choice.message
        messages.append(msg.model_dump())

        if not msg.tool_calls:
            summary = msg.content or "(no summary)"
            if had_write:
                if not all_succeeded:
                    rollback = txn.rollback()
                    return _finalize_subagent_result(
                        summary + f"\n\n[Subagent rolled back changes due to tool errors:\n{rollback}]",
                        scratch_path,
                        scratch_rel,
                        "tool_error_rolled_back",
                    )
                verification = dispatch("verify_changed_files", {"include_tests": False})
                if not _verification_passed(verification):
                    rollback = txn.rollback()
                    return _finalize_subagent_result(
                        summary + f"\n\n[Subagent rolled back changes after verification failure:\n{rollback}]",
                        scratch_path,
                        scratch_rel,
                        "verification_failed_rolled_back",
                        _verification_summary(verification),
                    )
                txn.commit()
                return _finalize_subagent_result(
                    summary,
                    scratch_path,
                    scratch_rel,
                    "completed",
                    _verification_summary(verification),
                )
            return _finalize_subagent_result(summary, scratch_path, scratch_rel, "completed")

        for tc in msg.tool_calls:
            fn = tc.function
            is_write = fn.name in ("write_file", "edit_file")
            try:
                args = json.loads(fn.arguments) if isinstance(fn.arguments, str) else (fn.arguments or {})
                if not isinstance(args, dict):
                    output = f"Error: Invalid arguments for {fn.name}: expected object"
                else:
                    output = _run_allowed_tool(fn.name, args, agent_type, txn, scratch_rel)
            except Exception as e:
                output = f"Error: {e}"
            raw_output = str(output)
            raw_failed = raw_output.startswith("Error:")
            output = persist_large_output(f"subagent-{tc.id}", raw_output)
            if is_write:
                had_write = True
                if raw_failed:
                    all_succeeded = False
            print(f"  [sub] {fn.name}: {output[:120]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})

    if had_write:
        rollback = txn.rollback()
        suffix = f"\n{rollback}" if rollback else ""
        return _timeout_message(f"max turns reached ({max_turns})") + suffix
    return _timeout_message(f"max turns reached ({max_turns})")


register(
    name="task",
    description="Spawn a subagent with fresh context for isolated exploration or work. Returns only a summary.",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Task description for the subagent."},
            "agent_type": {
                "type": "string",
                "enum": ["explore", "review", "test", "general"],
                "description": "explore/review = read-only, test = run checks without edits, general = may edit. Default: explore.",
            },
        },
        "required": ["prompt"],
    },
    handler=run_subagent,
)
