"""Thread-safe bridge from blocking tool handlers to async terminal selectors."""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
import threading

from nz_coder.tool_platform.permissioning.interaction import format_tool_summary


class TerminalInteractionBridge:
    """Expose synchronous Agent askers backed by the CLI event loop."""

    def __init__(self, terminal_input, renderer, loop: asyncio.AbstractEventLoop) -> None:
        self.terminal_input = terminal_input
        self.renderer = renderer
        self.loop = loop
        self.owner_thread = threading.get_ident()

    def ask_permission(self, tool_name: str, tool_input: dict) -> str:
        """Return once/always/reject without blocking the event-loop thread."""
        return self._submit(
            self._ask_permission(tool_name, tool_input),
            default="reject",
        )

    def ask_question(self, questions: list[dict]) -> list[list[str]] | None:
        """Return normalized structured answers or None when dismissed."""
        return self._submit(self._ask_questions(questions), default=None)

    def ask_workflow_approval(self, summary: dict) -> str:
        """Return approve/deny/cancel for one digest-bound Workflow summary."""
        return self._submit(
            self._ask_workflow_approval(dict(summary)), default="cancel"
        )

    def _submit(self, coroutine, *, default):  # noqa: ANN001
        if threading.get_ident() == self.owner_thread or self.loop.is_closed():
            coroutine.close()
            return default
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result()
        except (FutureCancelledError, RuntimeError):
            future.cancel()
            return default

    async def _ask_permission(self, tool_name: str, tool_input: dict) -> str:
        summary = format_tool_summary(tool_name, tool_input)
        reason = _permission_reason(tool_name, tool_input)
        result = await self._select(
            title="Permission required",
            text=(
                f"{summary}\nReason: {reason}\n"
                "Choose a scoped decision. Esc rejects."
            ),
            values=[
                ("once", "Allow once"),
                ("always", "Always allow this tool or command prefix"),
                ("reject", "Reject"),
            ],
        )
        return str(result) if result in {"once", "always", "reject"} else "reject"

    async def _ask_questions(self, questions: list[dict]) -> list[list[str]] | None:
        answers: list[list[str]] = []
        for question in questions:
            values = [
                (
                    str(option["label"]),
                    _question_option_label(option),
                )
                for option in question["options"]
            ]
            multiple = bool(question.get("multiple"))
            result = await self._select(
                title=str(question.get("header") or "Question"),
                text=(
                    f"{question['question']}\n"
                    + (
                        "Type to filter · Space toggles choices · Enter submits · Esc dismisses"
                        if multiple
                        else "Type to filter or enter a custom answer · Enter selects · Esc dismisses"
                    )
                ),
                values=values,
                multiple=multiple,
                allow_custom=True,
            )
            if result is None:
                return None
            if multiple:
                normalized = [str(value) for value in result]
            else:
                normalized = [str(result)]
            if not normalized:
                return None
            answers.append(normalized)
        return answers

    async def _ask_workflow_approval(self, summary: dict) -> str:
        phases = ", ".join(str(item) for item in summary.get("phases") or [])
        risk = "may write files" if summary.get("writes_files") else "read-only"
        text = (
            f"{summary.get('name', 'workflow')} — {summary.get('description', '')}\n"
            f"Phases: {phases or 'unspecified'}\n"
            f"Agents: {summary.get('planned_agents', 'unspecified')} planned, "
            f"{summary.get('max_concurrency', 'unspecified')} concurrent\n"
            f"Risk: {risk}\nApprove this exact plan?"
        )
        result = await self._select(
            title="Workflow approval",
            text=text,
            values=[
                ("approve", "Approve and start"),
                ("deny", "Deny this workflow"),
                ("cancel", "Cancel without starting"),
            ],
        )
        return str(result) if result in {"approve", "deny", "cancel"} else "cancel"

    async def _select(self, **kwargs):  # noqa: ANN003
        self.renderer.pause()
        try:
            return await self.terminal_input.select_async(**kwargs)
        finally:
            self.renderer.resume()


def bind_terminal_interactions(agent, terminal_input, renderer) -> TerminalInteractionBridge:
    """Attach one bridge to an Agent created for the active CLI event loop."""
    bridge = TerminalInteractionBridge(
        terminal_input,
        renderer,
        asyncio.get_running_loop(),
    )
    agent.set_interaction_askers(
        question_asker=bridge.ask_question,
        permission_asker=bridge.ask_permission,
        workflow_approval_asker=bridge.ask_workflow_approval,
    )
    return bridge


def _question_option_label(option: dict) -> str:
    label = str(option.get("label") or "")
    description = str(option.get("description") or "").strip()
    return f"{label} — {description}" if description else label


def _permission_reason(tool_name: str, tool_input: dict) -> str:
    """Explain user-visible risk without exposing internal policy records."""
    name = str(tool_name or "").lower()
    command = str(tool_input.get("command") or "").lower()
    if name == "bash" and any(token in command for token in ("install", "add ", "remove ")):
        return "Package installation may modify project dependencies or the environment."
    if name in {"write_file", "edit_file", "replace_lines", "apply_patch"}:
        return "This operation changes files in the current workspace."
    if name in {"bash", "process"}:
        return "This command starts a process with your workspace permissions."
    return "This operation has side effects and requires your approval."
