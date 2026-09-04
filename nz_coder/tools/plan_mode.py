"""Model-callable Plan/Build mode transitions with a session-local plan file."""
from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.sessions import session_plan_path
from nz_coder.tools import ToolOutput, register


_MAX_PLAN_CHARS = 100_000
_PLAN_CONTROLLER: ContextVar["PlanModeController | None"] = ContextVar(
    "nz_coder_plan_mode_controller",
    default=None,
)


@contextmanager
def scoped_plan_mode_controller(controller: "PlanModeController | None"):
    """Bind one agent's Plan/Build controller to its tool execution context."""
    token = _PLAN_CONTROLLER.set(controller)
    try:
        yield controller
    finally:
        _PLAN_CONTROLLER.reset(token)


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace the plan artifact without exposing partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


class PlanModeController:
    """Own Plan/Build transitions while leaving tool handlers dependency-free."""

    def __init__(
        self,
        permissions: Any,
        *,
        session_id: str,
        question_asker=None,
    ) -> None:
        self.permissions = permissions
        self.question_asker = question_asker
        self.plan_path = self._validated_path(session_plan_path(session_id))
        self._build_mode = (
            permissions.mode if permissions.mode != "plan" else "default"
        )
        self._pending_build_mode: str | None = None
        self._pending_terminal_summary = ""
        self._pending_exit_terminal = False

    @staticmethod
    def _validated_path(path: Path) -> Path:
        from nz_coder.foundation.user_paths import prepare_user_storage

        allowed = prepare_user_storage(current_workdir()).workspace_state / "sessions" / "_plans"
        target = path.absolute()
        try:
            target.relative_to(allowed.absolute())
        except ValueError as exc:
            raise ValueError("Plan path escapes private session state") from exc
        if target.is_symlink():
            raise ValueError("Plan path must not be an alias")
        return target

    def _display_path(self) -> str:
        return f"user-state://plans/{self.plan_path.name}"

    def _ask(self, question: dict) -> tuple[str, str]:
        if self.question_asker is None:
            return "error", "Interactive plan approval is unavailable"
        try:
            answers = self.question_asker([question])
        except Exception as exc:
            return "error", f"Interactive plan approval failed: {exc}"
        if answers is None:
            return "dismissed", ""
        if (
            not isinstance(answers, list)
            or len(answers) != 1
            or not isinstance(answers[0], list)
            or not all(isinstance(value, str) for value in answers[0])
        ):
            return "error", "Interactive plan approval returned malformed answers"
        values = [value.strip() for value in answers[0] if value.strip()]
        return "answered", values[0] if values else ""

    def enter(self, reason: str) -> str:
        reason = str(reason or "").strip()
        if not reason:
            return "Error: plan_enter requires a non-empty reason"
        if self.permissions.mode == "plan":
            return f"Plan mode is already active. Plan file: {self._display_path()}"

        approve_label = "Switch to Plan (Recommended)"
        status, answer = self._ask({
            "header": "Plan mode",
            "question": (
                "Switch to read-only Plan mode before implementation?\n"
                f"Reason: {reason[:500]}"
            ),
            "options": [
                {
                    "label": approve_label,
                    "description": "Research and write a reviewable plan before changing code.",
                },
                {
                    "label": "Continue Build",
                    "description": "Stay in the current mode and implement directly.",
                },
            ],
            "multiple": False,
        })
        if status == "error":
            return f"Error: {answer}"
        if status == "dismissed":
            return "User dismissed the Plan mode request. Remain in Build mode."
        if answer != approve_label:
            detail = f" Response: {answer}" if answer else ""
            return f"Plan mode was not entered.{detail}"

        self._build_mode = self.permissions.mode
        self._pending_build_mode = None
        self._pending_terminal_summary = ""
        self._pending_exit_terminal = False
        _atomic_write(self._validated_path(self.plan_path), "")
        self.permissions.mode = "plan"
        return (
            f"Plan mode is active. Plan file: {self._display_path()}. "
            "Inspect the repository without modifying source files, write the complete "
            "plan with write_plan, then call plan_exit."
        )

    def write(self, content: str) -> str:
        if self.permissions.mode != "plan":
            return "Error: write_plan is only available while Plan mode is active"
        if not isinstance(content, str) or not content.strip():
            return "Error: plan content must be a non-empty string"
        if len(content) > _MAX_PLAN_CHARS:
            return f"Error: plan content exceeds {_MAX_PLAN_CHARS} characters"
        normalized = content.rstrip() + "\n"
        _atomic_write(self._validated_path(self.plan_path), normalized)
        return (
            f"Plan updated at {self._display_path()} "
            f"({len(normalized)} characters)."
        )

    def exit(self, title: str, summary: str) -> str:
        if self.permissions.mode != "plan":
            return "Error: plan_exit is only available while Plan mode is active"
        title = str(title or "").strip()
        summary = str(summary or "").strip()
        if not title or not summary:
            return "Error: plan_exit requires non-empty title and summary"
        try:
            content = self._validated_path(self.plan_path).read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: unable to read the plan file: {exc}"
        if not content.strip():
            return "Error: plan file is empty; call write_plan before plan_exit"

        digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]
        approve_label = "Approve Plan (Recommended)"
        implement_label = "Implement in This Session"
        status, answer = self._ask({
            "header": "Plan ready",
            "question": f"{title[:200]}\n\n{summary[:1200]}",
            "options": [
                {
                    "label": approve_label,
                    "description": "Approve and finish this planning run.",
                },
                {
                    "label": implement_label,
                    "description": "Approve and immediately continue implementation.",
                },
                {
                    "label": "Keep Planning",
                    "description": "Stay read-only and revise the plan before implementation.",
                },
            ],
            "multiple": False,
        })
        if status == "error":
            return f"Error: {answer}"
        if status == "dismissed":
            return (
                "User dismissed plan approval. Stay in Plan mode and ask what should "
                "change before calling plan_exit again."
            )
        if answer not in {approve_label, implement_label}:
            detail = f" Response: {answer}" if answer else ""
            return f"Plan was not approved; remain in Plan mode.{detail}"

        try:
            reviewed = self._validated_path(self.plan_path).read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: unable to re-read the approved plan file: {exc}"
        reviewed_digest = hashlib.sha256(
            reviewed.strip().encode("utf-8")
        ).hexdigest()[:16]
        if reviewed_digest != digest:
            return (
                "Plan changed during approval. Remain in Plan mode, re-read the plan, "
                "and call plan_exit again with an updated title and summary."
            )

        self._pending_build_mode = self._build_mode
        self._pending_exit_terminal = answer == approve_label
        self._pending_terminal_summary = (
            f"Plan approved: {title}\n\n{summary}\n\n"
            f"Plan file: `{self._display_path()}`"
        )
        return ToolOutput(
            (
                f"Plan approved at {self._display_path()} (sha256:{digest}). "
                "Build mode will activate after this tool batch. Do not call "
                "implementation tools in the same response."
            ),
            title="Plan approved",
            metadata={
                "plan_exit_approved": True,
                "plan_exit_terminal": self._pending_exit_terminal,
                "title": title,
                "summary": summary,
                "plan_path": self._display_path(),
                "plan_digest": digest,
            },
        )

    @property
    def pending_terminal_summary(self) -> str:
        """Return the approved product-authored terminal presentation."""
        return self._pending_terminal_summary

    @property
    def pending_exit_terminal(self) -> bool:
        """Return whether approval ends planning instead of continuing Build."""
        return self._pending_exit_terminal

    def apply_pending_mode(self) -> tuple[str, str] | None:
        """Apply an approved exit only after every call in its tool batch completes."""
        target = self._pending_build_mode
        self._pending_build_mode = None
        if target is None or self.permissions.mode != "plan":
            return None
        previous = self.permissions.mode
        self.permissions.mode = target
        return previous, target

    def prompt_block(self) -> str:
        if self.permissions.mode != "plan":
            return ""
        return (
            "<plan-mode>\n"
            "Plan mode is ACTIVE. Do not modify source files, configuration, or system "
            "state. You may inspect the workspace, ask requirement questions, and write "
            f"only the dedicated plan file via write_plan: {self._display_path()}.\n"
            "Produce a concise but implementation-ready plan. When all questions are "
            "resolved, call plan_exit with a short title and Markdown bullet summary. "
            "Do not use question to ask whether the plan is approved.\n"
            "</plan-mode>"
        )


def _active_controller() -> PlanModeController | str:
    controller = _PLAN_CONTROLLER.get()
    if controller is None:
        return "Error: Plan mode service unavailable in this execution context"
    return controller


def plan_enter(reason: str) -> str:
    """Request a user-approved transition from Build mode to Plan mode."""
    try:
        controller = _active_controller()
        if isinstance(controller, str):
            return controller
        return controller.enter(reason)
    except Exception as exc:
        return f"Error: {exc}"


def write_plan(content: str) -> str:
    """Replace the active session's dedicated plan document."""
    try:
        controller = _active_controller()
        if isinstance(controller, str):
            return controller
        return controller.write(content)
    except Exception as exc:
        return f"Error: {exc}"


def plan_exit(title: str, summary: str) -> str:
    """Present the completed plan for approval and schedule Build mode."""
    try:
        controller = _active_controller()
        if isinstance(controller, str):
            return controller
        return controller.exit(title, summary)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="plan_enter",
    description=(
        "Ask the user to switch from Build mode to read-only Plan mode. Call this "
        "first when the user explicitly asks for a plan, or for complex multi-file "
        "work that needs research before implementation. Do not call it for simple "
        "tasks or when immediate implementation was requested."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why planning is useful before implementation.",
            },
        },
        "required": ["reason"],
    },
    handler=plan_enter,
    execution="serial",
    plan_mode_allowed=True,
)

register(
    name="write_plan",
    description=(
        "Write or replace the dedicated session plan file while Plan mode is active. "
        "This is the only write operation allowed during planning."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The complete implementation-ready Markdown plan.",
            },
        },
        "required": ["content"],
    },
    handler=write_plan,
    execution="serial",
    plan_mode_allowed=True,
)

register(
    name="plan_exit",
    description=(
        "Signal that the plan file is complete and present it for user approval. "
        "Always provide a concise title and Markdown bullet summary. Do not call this "
        "while requirements are unresolved."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short human-readable plan title."},
            "summary": {
                "type": "string",
                "description": "Concise Markdown bullet list of key steps and outcomes.",
            },
        },
        "required": ["title", "summary"],
    },
    handler=plan_exit,
    execution="serial",
    plan_mode_allowed=True,
)
