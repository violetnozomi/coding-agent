"""Thread-safe bridge from blocking tool handlers to async terminal selectors."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
import threading

from nz_coder.tool_platform.permissioning.interaction import (
    PermissionInteractionError, format_tool_summary, normalize_permission_reply,
)
from nz_coder.tool_platform.policies.command_policy import classify_bash
from nz_coder.tools import current_tool_cancel_event
from nz_coder.tools.question import QuestionWaitEnded


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
            default="reject", kind="permission",
        )

    def ask_question(self, questions: list[dict]) -> list[list[str]] | None:
        """Return normalized structured answers or None when dismissed."""
        return self._submit(self._ask_questions(questions), default=None, kind="question")

    def ask_workflow_approval(self, summary: dict) -> str:
        """Return approve/deny/cancel for one digest-bound Workflow summary."""
        return self._submit(
            self._ask_workflow_approval(dict(summary)), default="cancel", kind="workflow",
        )

    async def ask_auto_permission(
        self,
        tool_name: str,
        tool_input: dict,
        details: dict,
    ) -> str:
        """Settle one classifier fallback directly on the CLI event loop."""
        summary = format_tool_summary(tool_name, tool_input)
        reason = str(details.get("reason") or "Classifier unavailable")[:500]
        degraded = bool(details.get("degraded"))
        degraded_notice = (
            "Auto classifier is degraded; rules and human approval are active.\n"
            if degraded
            else ""
        )
        try:
            result = await self._select_permission(
                title="Auto mode permission required",
                detail=_permission_detail(tool_input),
                text=(
                    f"{summary}\nReason: {reason}\n"
                    f"{degraded_notice}"
                    "PgUp/PgDn scroll arguments. Choose a decision for this exact action. Esc rejects."
                ),
                values=[
                    ("once", "Allow once"),
                    (
                        "always",
                        "Always allow this exact action in this session",
                    ),
                    ("reject", "Reject"),
                ],
            )
        except Exception:
            raise PermissionInteractionError("unavailable") from None
        return "reject" if result is None else normalize_permission_reply(result)

    def _submit(self, coroutine, *, default, kind):  # noqa: ANN001
        if threading.get_ident() == self.owner_thread or self.loop.is_closed():
            coroutine.close()
            if kind == "permission":
                raise PermissionInteractionError("unavailable")
            return default
        cancel_event = current_tool_cancel_event()
        interaction_task = None

        async def run_interaction():
            nonlocal interaction_task
            interaction_task = asyncio.current_task()
            if cancel_event is not None and cancel_event.is_set():
                coroutine.close()
                if kind == "permission":
                    raise PermissionInteractionError("cancelled")
                if kind == "question":
                    raise QuestionWaitEnded("cancelled")
                return default
            return await coroutine

        def cancel_interaction():
            if interaction_task is not None:
                interaction_task.cancel()

        future = asyncio.run_coroutine_threadsafe(run_interaction(), self.loop)
        cancel_requested = False
        try:
            while True:
                try:
                    return future.result(timeout=0.05)
                except FutureTimeoutError:
                    if future.done():
                        return future.result()
                    if cancel_event is not None and cancel_event.is_set() and not cancel_requested:
                        cancel_requested = True
                        # 取消循环内的 Task，保留同一个 future 等待 finally 真正退出。
                        self.loop.call_soon_threadsafe(cancel_interaction)
        except (PermissionInteractionError, QuestionWaitEnded):
            raise
        except (FutureCancelledError, RuntimeError):
            future.cancel()
            cancelled = cancel_event is not None and cancel_event.is_set()
            if kind == "permission":
                reason = "cancelled" if cancelled else "unavailable"
                raise PermissionInteractionError(reason) from None
            if kind == "question" and cancelled:
                raise QuestionWaitEnded("cancelled") from None
            return default

    async def _ask_permission(self, tool_name: str, tool_input: dict) -> str:
        summary = format_tool_summary(tool_name, tool_input)
        reason = _permission_reason(tool_name, tool_input)
        result = await self._select_permission(
            title="Permission required",
            detail=_permission_detail(tool_input),
            text=(
                f"{summary}\nReason: {reason}\n"
                "PgUp/PgDn scroll arguments. Choose a scoped decision. Esc rejects."
            ),
            values=[
                ("once", "Allow once"),
                ("always", "Always allow this tool or command prefix"),
                ("reject", "Reject"),
            ],
        )
        return "reject" if result is None else normalize_permission_reply(result)

    async def _select_permission(self, **kwargs):
        try:
            return await self._select(**kwargs, require_interaction=True)
        except PermissionInteractionError:
            raise
        except Exception:
            raise PermissionInteractionError("unavailable") from None

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
                detail=str(question["question"]),
                text=(
                    "PgUp/PgDn scroll details · "
                    + (
                        "Type to filter · Space toggles · Enter submits · Esc dismisses"
                        if multiple
                        else "Type to filter/custom answer · Enter selects · Esc dismisses"
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
        auto_permission_asker=(
            bridge.ask_auto_permission
            if bool(getattr(terminal_input, "interactive", False))
            else None
        ),
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
    command = str(tool_input.get("command") or "")
    if name == "bash" and classify_bash(command)["reason"] in {"package install", "package manager write"}:
        return "Package installation may modify project dependencies or the environment."
    if name in {"write_file", "edit_file", "replace_lines", "apply_patch"}:
        return "This operation changes files in the current workspace."
    if name in {"bash", "process"}:
        return "This command starts a process with your workspace permissions."
    return "This operation has side effects and requires your approval."


def _permission_detail(tool_input: dict) -> str:
    """完整参数进入已有的滚动详情区，短摘要只用于快速识别操作。"""
    return "```json\n" + json.dumps(tool_input, ensure_ascii=False, indent=2) + "\n```"
