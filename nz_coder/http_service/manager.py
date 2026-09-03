"""In-process session ownership for the optional local HTTP transport."""
from __future__ import annotations

import asyncio
import copy
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import (
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    MESSAGE_SCHEMA_VERSION,
    attach_message_identity,
    ensure_message_identities,
    legacy_messages,
    message_records,
    settle_interrupted_parts,
    session_diffs,
    session_summary,
)
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.session.lifecycle import delete_session
from nz_coder.sdk import AgentClient
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.protocol.public_error import public_error_message, to_public_error
from nz_coder.state.sessions import (
    create_session_id,
    list_sessions,
    load_session,
    rename_session,
    save_session,
    session_snapshot_dir,
    session_runtime_dir,
)
from nz_coder.tool_platform.permissioning.modes import MODES

from .interactions import InteractionBroker
from .workspaces import WorkspaceRegistry

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_SAVED_SESSION_BYTES = 16 * 1024 * 1024


class SessionNotFoundError(LookupError):
    """Raised when an HTTP session ID is unknown to this service process."""


class SessionBusyError(RuntimeError):
    """Raised when an operation conflicts with an active Agent run."""


AgentFactory = Callable[[str, str], Any]


def _validated_wait_timeout(
    value: float | None,
    label: str,
    *,
    allow_none: bool,
) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"session {label} timeout must be a non-negative finite number")
    if isinstance(value, bool):
        raise ValueError(f"session {label} timeout must be a non-negative finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"session {label} timeout must be a non-negative finite number"
        ) from exc
    if not math.isfinite(timeout) or timeout < 0 or timeout > 86_400:
        raise ValueError(
            f"session {label} timeout must be a non-negative finite number"
        )
    return timeout


def _interaction_records(
    records: list[dict],
    interaction_run_id: str,
) -> list[dict]:
    """Select one interaction without guessing from message order."""
    if not interaction_run_id:
        return []
    return [
        copy.deepcopy(record)
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("info"), dict)
        and record["info"].get("interaction_run_id") == interaction_run_id
    ]


def build_http_agent(session_id: str, permission_mode: str):
    """Build an AgentLoop with a conservative pre-broker permission fallback."""
    from nz_coder.state.memory import (
        bind_memory_manager,
        workspace_memory_manager,
    )
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.execution.composition import build_coding_agent
    from nz_coder.runtime.process.workdir import current_derived_path, current_workdir
    from nz_coder.state.skills import SkillLoader, bind_skill_loader, current_skill_loader
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    memory_dir = current_derived_path("MEMORY_DIR")
    memory_manager = workspace_memory_manager(memory_dir)
    workspace_snapshot = load_config_snapshot(current_workdir())
    project_skills = current_workdir() / ".nz-coder" / "skills"
    default_skills = current_skill_loader()
    skill_manager = (
        default_skills
        if (
            default_skills._project_dir.resolve() == project_skills.resolve()
            and default_skills._workspace_trusted
            == workspace_snapshot.control_plane_trusted
        )
        else SkillLoader(
            project_dir=project_skills,
            workspace_trusted=workspace_snapshot.control_plane_trusted,
            project_control_snapshot=workspace_snapshot.project_control,
        )
    )
    memory_manager.load_all()
    system_prompt = build(
        memory_block="",
        skill_descriptions=skill_manager.descriptions(),
    )
    with bind_memory_manager(memory_manager), bind_skill_loader(skill_manager):
        agent = build_coding_agent(
            system_prompt,
            permission_mode=permission_mode,
            session_id=session_id,
            permission_asker=lambda _name, _input: False,
            event_bus=SessionEventBus(
                session_id=session_id,
                journal_path=session_runtime_dir(session_id) / "events.jsonl",
            ),
            config_snapshot=workspace_snapshot,
        )
    if not hasattr(agent, "config_snapshot"):
        agent.config_snapshot = workspace_snapshot
    return agent


class ManagedSession:
    """One workspace-bound Agent and its committed conversation history."""

    def __init__(
        self,
        session_id: str,
        permission_mode: str,
        agent: Any,
        run_gate: threading.Lock,
        interaction_timeout_seconds: float,
        *,
        workspace_id: str,
        workspace: Path,
        history: list[dict] | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
        restored: bool = False,
        initial_status: str = "idle",
        last_error: str = "",
        title: str = "New Session",
        model: str = "unknown",
        parent_session_id: str | None = None,
        client: AgentClient | None = None,
        event_bus: SessionEventBus | None = None,
    ):
        now = time.time()
        self.session_id = session_id
        self.permission_mode = permission_mode
        self.agent = agent
        self.client = client
        self.workspace_id = workspace_id
        self.workspace = workspace
        from nz_coder.foundation.workspace_trust import load_config_snapshot

        self.config_snapshot = getattr(agent, "config_snapshot", None)
        if self.config_snapshot is None:
            self.config_snapshot = load_config_snapshot(workspace)
        self.history = copy.deepcopy(history or [])
        ensure_message_identities(self.history, session_id)
        self.created_at = created_at if created_at is not None else now
        self.updated_at = updated_at if updated_at is not None else now
        self.restored = restored
        self.status = initial_status
        self.last_error = last_error
        self.title = str(title or "New Session")
        self.model = str(model or "unknown")
        self.parent_session_id = parent_session_id
        self.last_result: dict = {}
        self._lock = threading.RLock()
        self._run_thread: threading.Thread | None = None
        self._run_loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task | None = None
        self._run_phase = "idle"
        self._run_event_floor = 0
        self._active_interaction_run_id = ""
        self._active_event_publisher = None
        self._cancel_requested = False
        self._disposed = False
        self._run_gate = run_gate
        self._gate_acquired = False
        self._event_bus = event_bus or agent.event_bus
        self.interactions = InteractionBroker(
            session_id=session_id,
            event_bus=self.event_bus,
            timeout_seconds=interaction_timeout_seconds,
        )
        bind_interactions = getattr(self.agent, "set_interaction_askers", None)
        if callable(bind_interactions):
            bind_interactions(
                question_asker=self.interactions.ask_question,
                permission_asker=self.interactions.ask_permission,
            )

    @property
    def event_bus(self):
        return self._event_bus

    def _model_identity(self) -> tuple[str, str, str | None]:
        if self.agent is not None:
            return (
                str(getattr(self.agent, "provider_id", "unknown")),
                str(getattr(self.agent, "model_id", "unknown")),
                getattr(self.agent, "model_variant", None),
            )
        from nz_coder.providers.models import active_model_selection

        selected = active_model_selection(self.workspace)
        return selected.provider, selected.model_id, selected.variant

    def _run_request(
        self,
        messages: list[dict],
        allowed_tools: tuple[str, ...] = (),
        model_override: str | None = None,
    ) -> RunRequest:
        from nz_coder.runtime.conversation.prompt import build

        provider, model, variant = self._model_identity()
        if model_override:
            value = str(model_override).strip()
            if "/" in value:
                provider, model = value.split("/", 1)
            else:
                model = value
        return RunRequest(
            agent=AgentDefinition(
                name="worker",
                instructions=build(memory_block="", skill_descriptions=""),
                provider=provider,
                model=model,
                reasoning_effort=variant,
                allowed_tools=allowed_tools or None,
            ),
            profile=MAIN_PROFILE,
            messages=messages,
            workspace=self.workspace,
            session_id=self.session_id,
            tool_names=allowed_tools,
            stream=True,
            interaction_run_id=self._active_interaction_run_id or None,
            provider=provider,
            model=model,
            reasoning_effort=variant,
            metadata={
                "permission_mode": self.permission_mode,
                "persist_session": True,
                # The HTTP manager owns the committed transcript and status.
                "product_surface": "http",
                "interaction_run_id": self._active_interaction_run_id,
            },
        )

    def info(self) -> dict:
        with self._lock:
            runtime_status = self.status
            pending = self.interactions.list()
            kinds = {str(item.get("kind") or "") for item in pending}
            if self._run_thread is not None and "permission" in kinds:
                runtime_status = "waiting_permission"
            elif self._run_thread is not None and "question" in kinds:
                runtime_status = "waiting_question"
            provider, _model, variant = self._model_identity()
            return {
                "id": self.session_id,
                "status": self.status,
                "runtime_status": runtime_status,
                "title": self.title,
                "model": self.model,
                "provider": provider,
                "reasoning_effort": variant,
                "mode": self.permission_mode,
                "parent_session_id": self.parent_session_id,
                "permission_mode": self.permission_mode,
                "workspace_id": self.workspace_id,
                "workspace": str(self.workspace),
                "restored": self.restored,
                "message_count": len(self.history),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "running": self._run_thread is not None,
                "pending_interaction_count": len(pending),
                "last_error": self.last_error,
                "last_result": copy.deepcopy(self.last_result),
                "active_interaction_run_id": self._active_interaction_run_id,
            }

    def rename(self, title: str) -> str:
        """Persist a Session title without introducing a remote-side state copy."""
        with self._lock:
            if self._run_thread is not None:
                raise SessionBusyError("cannot rename an active session run")
            with scoped_workdir(self.workspace):
                normalized = rename_session(self.session_id, title)
            self.title = normalized
            self.updated_at = time.time()
            return normalized

    def _reverter(self):
        from nz_coder.runtime.session.session_revert import SessionReverter
        from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore

        with scoped_workdir(self.workspace):
            snapshot_root = session_snapshot_dir(self.session_id)
            state_path = session_runtime_dir(self.session_id) / "message_revert.json"
        return SessionReverter(
            WorkspaceSnapshotStore(self.workspace, snapshot_root),
            state_path,
        )

    def undo(self) -> dict:
        with self._lock:
            if self._run_thread is not None:
                raise SessionBusyError("cannot undo during an active session run")
            result = self._reverter().revert(self.history)
            self._persist_transition_locked()
            return {"message_id": result.message_id, "files": list(result.files), "removed_messages": result.removed_messages}

    def redo(self) -> dict:
        with self._lock:
            if self._run_thread is not None:
                raise SessionBusyError("cannot redo during an active session run")
            result = self._reverter().unrevert(self.history)
            self._persist_transition_locked()
            return {"message_id": result.message_id, "files": list(result.files), "removed_messages": result.removed_messages}

    def _persist_transition_locked(self) -> None:
        with scoped_workdir(self.workspace):
            save_session(
                self._persistence_messages(),
                mode=self.permission_mode,
                session_id=self.session_id,
                activate=False,
                run_status=self.status,
                require_aliases=False,
                title=self.title,
                parent_session_id=self.parent_session_id,
                model=self.model,
            )
        self.updated_at = time.time()

    def messages(self) -> list[dict]:
        with self._lock:
            return legacy_messages(self.history)

    def diff(self) -> list[dict]:
        """Return the latest bounded snapshot-derived Session diff."""
        with self._lock:
            return session_diffs(self.history)

    def _persistence_messages(self) -> list[dict]:
        """Return history including additive identity metadata for storage."""
        with self._lock:
            return copy.deepcopy(self.history)

    def snapshot(self) -> dict:
        """Return an idle message snapshot and an atomic SSE resume cursor."""
        with self._lock:
            if self._run_thread is not None or self.status == "running":
                raise SessionBusyError("session snapshot requires an idle run")
            snapshot_id = f"snap-{uuid.uuid4().hex}"

            def capture() -> dict:
                return {
                    "schema_version": MESSAGE_SCHEMA_VERSION,
                    "snapshot_id": snapshot_id,
                    "session": self.info(),
                    "summary": session_summary(self.history),
                    "messages": message_records(self.history, self.session_id),
                    "pending": {"permissions": [], "questions": []},
                }

            result, cursor = self.event_bus.checkpoint(
                capture,
                event_type="session.snapshot.created",
                properties={
                    "snapshot_id": snapshot_id,
                    "message_count": len(self.history),
                },
            )
            result["cursor"] = {
                "event_id": cursor.event_id,
                "sequence": cursor.sequence,
            }
            return result

    def attach_snapshot(self) -> dict:
        """Return a running-safe baseline and an atomic SSE resume cursor.

        A normal snapshot represents a settled transcript and therefore stays
        idle-only.  An attach baseline intentionally captures the last
        committed transcript plus pending interactions while a run may still
        be producing events.  The event-bus checkpoint makes the baseline and
        resume cursor atomic, so a remote terminal cannot miss the boundary.
        """
        with self._lock:
            snapshot_id = f"attach-{uuid.uuid4().hex}"
            pending = {
                "permissions": self.interactions.list("permission"),
                "questions": self.interactions.list("question"),
            }

            def capture() -> dict:
                timeline_messages = message_records(
                    self.history,
                    self.session_id,
                )
                run_messages = _interaction_records(
                    timeline_messages,
                    self._active_interaction_run_id,
                )
                return {
                    "schema_version": MESSAGE_SCHEMA_VERSION,
                    "snapshot_id": snapshot_id,
                    "settled": self._run_thread is None and self.status != "running",
                    "session": self.info(),
                    "summary": session_summary(self.history),
                    "messages": timeline_messages,
                    "active_interaction_run_id": self._active_interaction_run_id,
                    "run": {
                        "interaction_run_id": self._active_interaction_run_id,
                        "status": self.info()["runtime_status"],
                        "message_ids": [
                            record["info"]["id"] for record in run_messages
                        ],
                        "messages": run_messages,
                        "parts": [
                            copy.deepcopy(part)
                            for record in run_messages
                            for part in record.get("parts", [])
                        ],
                        "pending": copy.deepcopy(pending),
                    },
                    "timeline": {"messages": timeline_messages},
                    "pending": copy.deepcopy(pending),
                }

            result, cursor, replay_events = self.event_bus.checkpoint_with_replay(
                capture,
                event_type="session.attach.snapshot.created",
                properties={
                    "snapshot_id": snapshot_id,
                    "message_count": len(self.history),
                },
                replay=256,
                publisher=self._active_event_publisher,
            )
            result["pending"] = _merge_replayed_interactions(
                result["pending"], replay_events
            )
            result["run"]["pending"] = copy.deepcopy(result["pending"])
            result["run"]["snapshot_sequence"] = cursor.sequence
            if self._run_thread is not None or self.status == "running":
                result["events"] = [
                    event.to_dict()
                    for event in replay_events
                    if event.sequence > self._run_event_floor
                    and (
                        not self._active_interaction_run_id
                        or event.run_id == self._active_interaction_run_id
                    )
                ]
            else:
                result["events"] = []
            result["cursor"] = {
                "event_id": cursor.event_id,
                "sequence": cursor.sequence,
            }
            return result

    def commands(self) -> list[dict]:
        """Project runtime-workspace commands without caching client truth."""
        from nz_coder.interface.custom_commands import default_command_catalog

        return [
            {
                "name": item.name,
                "description": item.description,
                "source": item.source,
                "allowed_tools": list(item.allowed_tools),
                "model": item.model,
            }
            for item in default_command_catalog(
                self.workspace,
                config_snapshot=self.config_snapshot,
            ).list()
        ]

    def extensions(self) -> list[dict]:
        """Project extension owners from this Session's daemon workspace."""
        from nz_coder.extensions.registry import ExtensionRegistry
        from nz_coder.state.skills import SkillLoader

        loader = SkillLoader(
            project_dir=self.workspace / ".nz-coder" / "skills",
            workspace_trusted=self.config_snapshot.control_plane_trusted,
            project_control_snapshot=self.config_snapshot.project_control,
        )
        return [
            item.to_dict()
            for item in ExtensionRegistry(
                workspace=self.workspace,
                skill_loader=loader,
            ).snapshot()
        ]

    def agents(self) -> list[dict]:
        """Project Agent definitions without creating a second Agent registry."""
        from nz_coder.interface.agent_catalog import agent_catalog

        return agent_catalog(self.agent, self.workspace)

    def _workflow_manager(self):  # noqa: ANN202
        manager = getattr(self.agent, "background_agents", None)
        if manager is None:
            raise ValueError("Session Workflow manager is unavailable")
        return manager

    def workflows(self) -> dict:
        """Project active and durable runs from the Session-owned Workflow owner."""
        from nz_coder.runtime.workflows.workflow_run_store import list_workflow_run_records

        manager = self._workflow_manager()
        active = manager.workflow_run_snapshots()
        active_ids = {str(item.get("run_id") or "") for item in active}
        persisted = [
            item
            for item in list_workflow_run_records(manager._workflow.root / "runs", 100)
            if str(item.get("run_id") or "") not in active_ids
        ]
        return {"runs": [*active, *persisted]}

    def workflow(self, run_id: str) -> dict:
        from nz_coder.runtime.workflows.workflow_run_store import read_workflow_run_record

        identifier = str(run_id or "").strip()
        manager = self._workflow_manager()
        active = next(
            (
                item for item in manager.workflow_run_snapshots()
                if str(item.get("run_id") or "") == identifier
            ),
            None,
        )
        if active is not None:
            return active
        return read_workflow_run_record(manager._workflow.root / "runs", identifier)

    def control_workflow(self, run_id: str, action: str) -> dict:
        """Delegate lifecycle changes to the canonical BackgroundAgentManager."""
        manager = self._workflow_manager()
        operation = {
            "pause": manager.pause_workflow_run,
            "resume": manager.resume_workflow_run,
            "stop": manager.stop_workflow_run,
        }.get(str(action))
        if operation is None:
            raise ValueError("workflow action must be pause, resume, or stop")
        if not operation(str(run_id)):
            raise ValueError(f"workflow cannot {action}: {run_id}")
        return self.workflow(str(run_id))

    def _resolve_workflow_for_approval(self, name: str, arguments: dict) -> tuple:
        """Resolve one plan once and bind its approval summary to that object."""
        import hashlib
        import json

        from nz_coder.runtime.workflows.workflow_host import (
            build_workflow_approval_summary,
            workflow_approval_digest,
        )
        from nz_coder.runtime.workflows.workflow_resolver import resolve_workflow_capsule

        if not isinstance(arguments, dict):
            raise ValueError("workflow arguments must be an object")
        manager = self._workflow_manager()
        resolved = resolve_workflow_capsule(
            str(name), arguments, workspace=manager.workspace
        )
        manifest = resolved["capsule"]["plan"].get("manifest") or {}
        summary = build_workflow_approval_summary(
            manifest,
            system_max_agents=manager.agent_cap,
            system_max_concurrency=manager.concurrency_cap,
        )
        summary["plan_digest"] = hashlib.sha256(json.dumps(
            resolved["capsule"]["plan"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        return manager, resolved, summary, workflow_approval_digest(summary)

    def prepare_workflow(self, name: str, arguments: dict) -> dict:
        """Resolve and fingerprint one exact data-only Workflow plan."""
        _manager, resolved, summary, approval_digest = (
            self._resolve_workflow_for_approval(name, arguments)
        )
        return {
            "name": str(name),
            "source": str(resolved["ref"].get("source") or ""),
            "summary": summary,
            "approval_digest": approval_digest,
        }

    def start_workflow(
        self,
        name: str,
        arguments: dict,
        approval_digest: str,
    ) -> dict:
        """Start the exact plan approved by a Remote product adapter."""
        import hmac

        from nz_coder.runtime.workflows.workflow_sdk import WorkflowHostSDK

        manager, resolved, _summary, expected = self._resolve_workflow_for_approval(
            name, arguments
        )
        provided = str(approval_digest or "")
        if not provided or not hmac.compare_digest(provided, expected):
            raise ValueError("workflow approval is stale or does not match this plan")
        handle = WorkflowHostSDK(manager).start(
            plan=resolved["capsule"]["plan"],
            display_name=str(resolved["capsule"]["manifest"].get("name") or name),
            approval_decision="approve",
            approval_digest=provided,
        )
        return handle.wait_started(10.0)

    def memory_control(self):  # noqa: ANN202
        from nz_coder.state.memory_control import MemoryControlPlane

        manager = getattr(self.agent, "_mm", None)
        if manager is None:
            raise ValueError("Session Memory manager is unavailable")
        return MemoryControlPlane(manager.memory_dir, manager)

    @staticmethod
    def _memory_proposal_dict(proposal) -> dict:  # noqa: ANN001
        from dataclasses import asdict

        value = asdict(proposal)
        value["source_message_ids"] = list(value.get("source_message_ids") or [])
        return value

    def memory_status(self) -> dict:
        control = self.memory_control()
        return {
            "pending": [self._memory_proposal_dict(item) for item in control.pending()],
            "ledger": control.ledger()[-100:],
        }

    def memory_proposal(self, fingerprint: str) -> dict:
        proposal = self.memory_control().get(str(fingerprint))
        if proposal is None:
            raise ValueError(f"memory proposal was not found: {fingerprint}")
        return self._memory_proposal_dict(proposal)

    def review_memory(self, fingerprint: str, action: str, reason: str = "") -> dict:
        control = self.memory_control()
        if action == "approve":
            proposal = control.approve(str(fingerprint), reviewer="remote-user")
        elif action == "reject":
            proposal = control.reject(
                str(fingerprint),
                reviewer="remote-user",
                reason=str(reason or "rejected by remote user"),
            )
        else:
            raise ValueError("memory action must be approve or reject")
        return self._memory_proposal_dict(proposal)

    def expand_command(self, name: str, arguments: str = "") -> dict:
        from nz_coder.interface.custom_commands import default_command_catalog

        if not isinstance(arguments, str):
            raise ValueError("command arguments must be a string")
        try:
            expanded = default_command_catalog(
                self.workspace,
                config_snapshot=self.config_snapshot,
            ).expand(name, arguments)
        except KeyError as exc:
            raise ValueError(f"custom command was not found: {name}") from exc
        return {
            "name": expanded.name,
            "prompt": expanded.prompt,
            "source": expanded.source,
            "allowed_tools": list(expanded.allowed_tools),
            "model": expanded.model,
        }

    def start_run(
        self,
        message: str,
        *,
        attachments=(),
        allowed_tools=(),
        model: str | None = None,
    ) -> dict:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if not isinstance(attachments, (list, tuple)):
            raise ValueError("attachments must be a list of workspace file paths")
        if len(attachments) > 20 or any(not isinstance(path, str) for path in attachments):
            raise ValueError("attachments must contain at most 20 file paths")
        if not isinstance(allowed_tools, (list, tuple)):
            raise ValueError("allowed_tools must be a list of tool names")
        if len(allowed_tools) > 100 or any(
            not isinstance(name, str) or not name.strip() for name in allowed_tools
        ):
            raise ValueError("allowed_tools must contain at most 100 tool names")
        selected_tools = tuple(dict.fromkeys(name.strip() for name in allowed_tools))
        model_override = str(model or "").strip() or None
        if model_override is not None and len(model_override) > 240:
            raise ValueError("model override is too long")
        with self._lock:
            if self._disposed:
                raise SessionNotFoundError(self.session_id)
            if self._run_thread is not None:
                raise SessionBusyError("session already has an active run")
            if not self._run_gate.acquire(blocking=False):
                raise SessionBusyError(
                    "another session already has an active run in this workspace"
                )
            self._gate_acquired = True
            previous_history = copy.deepcopy(self.history)
            try:
                self._active_interaction_run_id = (
                    f"interaction-{uuid.uuid4().hex}"
                )
                self._active_event_publisher = self.event_bus.for_interaction(
                    self._active_interaction_run_id,
                    agent_invocation_id=str(
                        getattr(self.agent, "agent_id", "worker") or "worker"
                    ),
                )
                self.interactions.begin_run(self._active_event_publisher)
                recent = self.event_bus.recent(1)
                self._run_event_floor = recent[-1].sequence if recent else 0
                provider_id, model_id, variant = self._model_identity()
                if model_override:
                    if "/" in model_override:
                        provider_id, model_id = model_override.split("/", 1)
                    else:
                        model_id = model_override
                from nz_coder.interface.submission import (
                    build_user_submission,
                    resolve_submission_files,
                )

                files = resolve_submission_files(attachments, self.workspace)
                user_message = build_user_submission(
                    message,
                    files,
                    workspace=self.workspace,
                    session_id=self.session_id,
                    agent="plan" if self.permission_mode == "plan" else "build",
                    provider_id=provider_id,
                    model_id=model_id,
                    variant=variant,
                )
                # Image/document submission construction may already create an
                # identity referenced by its FileParts. Re-keying that message
                # would orphan and subsequently discard those parts.
                if not isinstance(user_message.get(MESSAGE_ID_KEY), str):
                    attach_message_identity(user_message, session_id=self.session_id)
                user_message[INTERACTION_RUN_ID_KEY] = (
                    self._active_interaction_run_id
                )
                self.history.append(user_message)
                run_messages = copy.deepcopy(self.history)
                with scoped_workdir(self.workspace):
                    save_session(
                        self._persistence_messages(),
                        mode=self.permission_mode,
                        session_id=self.session_id,
                        activate=False,
                        run_status="running",
                        require_aliases=False,
                        model=self.model,
                    )
                self.status = "running"
                self.last_error = ""
                self.last_result = {}
                self.updated_at = time.time()
                self._cancel_requested = False
                self._run_phase = "starting"
                thread = threading.Thread(
                    target=self._run_agent,
                    args=(run_messages, selected_tools, model_override),
                    name=f"nz-http-{self.session_id}",
                    daemon=True,
                )
                self._run_thread = thread
                thread.start()
            except Exception as exc:
                self._run_thread = None
                self._run_phase = "idle"
                self._run_event_floor = 0
                self.status = "failed"
                self.last_error = to_public_error(exc).message
                self.history = previous_history
                try:
                    with scoped_workdir(self.workspace):
                        save_session(
                            self._persistence_messages(),
                            mode=self.permission_mode,
                            session_id=self.session_id,
                            activate=False,
                            run_status="failed",
                            require_aliases=False,
                            model=self.model,
                        )
                except Exception:
                    pass
                self.interactions.cancel_all("start_failed", block_new=True)
                self._release_run_gate()
                raise
            return self.info()

    def abort(self) -> bool:
        with self._lock:
            if (
                self._run_thread is None
                or self.status != "running"
                or self._run_phase not in {"starting", "running"}
                or self._cancel_requested
            ):
                return False
            self._cancel_requested = True
            loop = self._run_loop
            task = self._run_task
        self.interactions.cancel_all("aborted", block_new=True)
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        return True

    def wait(self, timeout: float | None = None) -> bool:
        wait_timeout = _validated_wait_timeout(timeout, "wait", allow_none=True)
        with self._lock:
            thread = self._run_thread
        if thread is None:
            return True
        thread.join(timeout=wait_timeout)
        return not thread.is_alive()

    def dispose(self, *, force: bool = False) -> None:
        with self._lock:
            if self._disposed:
                return
            if self._run_thread is not None and not force:
                raise SessionBusyError("abort the active run before deleting the session")
            self._disposed = True
            self.status = "disposed"
            self.updated_at = time.time()
        self.interactions.close()
        from nz_coder.runtime.process.process_service import dispose_session_processes

        dispose_session_processes(self.workspace, self.session_id)
        if self.agent is not None:
            self.agent.close()
        else:
            self.event_bus.close()

    def delete_persisted(self) -> bool:
        """Atomically reject active runs, delete owned state, then dispose."""
        with self._lock:
            if self._disposed:
                return False
            if self._run_thread is not None:
                raise SessionBusyError("abort the active run before deleting the session")
            with scoped_workdir(self.workspace):
                deleted = delete_session(self.session_id)
            try:
                self.event_bus.publish(
                    "session.deleted",
                    {"session_id": self.session_id, "info": self.info()},
                )
            except RuntimeError:
                pass
            self._disposed = True
            self.status = "disposed"
            self.updated_at = time.time()
        self.interactions.close()
        from nz_coder.runtime.process.process_service import dispose_session_processes

        dispose_session_processes(self.workspace, self.session_id)
        if self.agent is not None:
            self.agent.close()
        else:
            self.event_bus.close()
        return deleted

    def _run_agent(
        self,
        run_messages: list[dict],
        allowed_tools: tuple[str, ...] = (),
        model_override: str | None = None,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from nz_coder.foundation.workspace_trust import scoped_config_snapshot

        with scoped_workdir(self.workspace), scoped_config_snapshot(self.config_snapshot):
            if self.client is None:
                # The legacy Agent adapter must not infer an interaction identity
                # from the session-long EventBus. Bind the request-scoped identity
                # explicitly for this invocation only.
                self.agent._requested_interaction_run_id = (
                    self._active_interaction_run_id
                )
                task = loop.create_task(self.agent.run(run_messages, stream=True))
            else:
                task = loop.create_task(self.client.run(
                    self._run_request(run_messages, allowed_tools, model_override),
                    permission_asker=self.interactions.ask_permission,
                    question_asker=self.interactions.ask_question,
                    workflow_approval_asker=lambda _summary: "reject",
                    event_bus=self.event_bus,
                ))
        with self._lock:
            self._run_loop = loop
            self._run_task = task
            self._run_phase = "running"
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()

        status = "completed"
        error = ""
        result: dict = {}
        persisted = False
        try:
            value = loop.run_until_complete(task)
            if self.client is not None:
                run_messages[:] = copy.deepcopy(list(value.messages))
                status = value.status.value
                result = {
                    "status": status,
                    "answer": value.final_text,
                    "active_agent": value.active_agent,
                    "error": (
                        public_error_message(value.error)
                        if value.error
                        else ""
                    ),
                    "metadata": copy.deepcopy(value.metadata),
                }
            else:
                result = copy.deepcopy(value) if isinstance(value, dict) else {"result": value}
                status = str(result.get("status") or "completed")
                if status in {"error", "failed", "blocked", "aborted"}:
                    for key in ("error", "last_error"):
                        if result.get(key):
                            result[key] = public_error_message(result[key])
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            status = "failed"
            error = to_public_error(exc).message
        finally:
            if self.client is None:
                self.agent._requested_interaction_run_id = None
            with self._lock:
                self._run_phase = "committing"
                if self._cancel_requested:
                    status = "cancelled"
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            with self._lock:
                ensure_message_identities(run_messages, self.session_id)
                self.history = copy.deepcopy(run_messages)
                self.last_error = error
                self.last_result = result
                self.updated_at = time.time()
                self._run_loop = None
                self._run_task = None
            try:
                with scoped_workdir(self.workspace):
                    save_session(
                        self._persistence_messages(),
                        mode=self.permission_mode,
                        session_id=self.session_id,
                        activate=False,
                        run_status=status,
                        require_aliases=False,
                        model=self.model,
                    )
                persisted = True
                try:
                    payload = load_session(self.session_id)
                    with self._lock:
                        self.title = str(payload.get("title") or self.title)
                        # A custom-command override is immutable per RunRequest;
                        # it never becomes the Session's configured model.
                        self.model = str(payload.get("model") or self.model)
                        self.parent_session_id = payload.get("parent_session_id") or self.parent_session_id
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                with self._lock:
                    self.interactions.cancel_all("run_settled", block_new=True)
                    self.status = status
                    self.updated_at = time.time()
                    self._run_thread = None
                    self._run_phase = "idle"
                    self._run_event_floor = 0
                    self._release_run_gate()
                    try:
                        (self._active_event_publisher or self.event_bus).publish(
                            "session.run.settled",
                            {"status": status, "persisted": persisted},
                        )
                    except RuntimeError:
                        pass

    def _release_run_gate(self) -> None:
        if not self._gate_acquired:
            return
        self._gate_acquired = False
        self._run_gate.release()


def _merge_replayed_interactions(pending: dict, events: list) -> dict:
    """Fold the atomic replay suffix into a pre-checkpoint pending snapshot."""
    values = {
        "permission": {
            str(item.get("id")): copy.deepcopy(item)
            for item in pending.get("permissions", [])
            if isinstance(item, dict) and item.get("id")
        },
        "question": {
            str(item.get("id")): copy.deepcopy(item)
            for item in pending.get("questions", [])
            if isinstance(item, dict) and item.get("id")
        },
    }
    for event in events:
        props = event.properties
        if event.type in {"permission.asked", "question.asked"}:
            kind = event.type.split(".", 1)[0]
            request_id = str(props.get("id") or "")
            if request_id:
                values[kind][request_id] = copy.deepcopy(props)
        elif event.type in {
            "permission.replied", "question.replied", "question.rejected",
        }:
            kind = event.type.split(".", 1)[0]
            values[kind].pop(str(props.get("request_id") or ""), None)
    return {
        "permissions": sorted(
            values["permission"].values(),
            key=lambda item: (item.get("created_at", 0), item.get("id", "")),
        ),
        "questions": sorted(
            values["question"].values(),
            key=lambda item: (item.get("created_at", 0), item.get("id", "")),
        ),
    }


class SessionManager:
    """Own live and restorable sessions across authorized local workspaces."""

    def __init__(
        self,
        agent_factory: AgentFactory | None = None,
        *,
        interaction_timeout_seconds: float = 300.0,
        workspace_roots: list[str | Path] | None = None,
        restore_saved: bool = True,
        max_saved_sessions_per_workspace: int = 1000,
    ):
        self._agent_factory = agent_factory
        interaction_timeout = float(interaction_timeout_seconds)
        if not math.isfinite(interaction_timeout) or interaction_timeout <= 0:
            raise ValueError("interaction timeout must be a positive finite number")
        self._interaction_timeout_seconds = max(0.05, interaction_timeout)
        if (
            not isinstance(max_saved_sessions_per_workspace, int)
            or isinstance(max_saved_sessions_per_workspace, bool)
            or max_saved_sessions_per_workspace < 0
        ):
            raise ValueError("max saved sessions must be non-negative")
        self.workspaces = WorkspaceRegistry(workspace_roots)
        self._lock = threading.RLock()
        self._sessions: dict[str, ManagedSession] = {}
        self._saved: dict[str, dict] = {}
        self._closed = False
        self._run_gates = {
            item["id"]: threading.Lock() for item in self.workspaces.list()
        }
        if restore_saved and max_saved_sessions_per_workspace:
            self._discover_saved(max_saved_sessions_per_workspace)

    def create(
        self,
        permission_mode: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        mode = permission_mode or config.PERMISSION_MODE
        if mode not in MODES:
            raise ValueError(f"unsupported permission_mode: {mode}")
        workspace = self.workspaces.get(workspace_id)
        selected_workspace_id = self.workspaces.id_for(workspace)
        with self._lock:
            if self._closed:
                raise RuntimeError("session manager is closed")
            while True:
                session_id = create_session_id("http")
                if session_id not in self._sessions and session_id not in self._saved:
                    break
            session = self._build_session(
                session_id=session_id,
                mode=mode,
                workspace_id=selected_workspace_id,
                workspace=workspace,
            )
            self._sessions[session_id] = session
            try:
                with scoped_workdir(workspace):
                    save_session(
                        session._persistence_messages(),
                        mode=mode,
                        session_id=session_id,
                        activate=False,
                        run_status="idle",
                        require_aliases=False,
                    )
            except Exception:
                self._sessions.pop(session_id, None)
                session.dispose(force=True)
                raise
            return session.info()

    def list(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
            saved = [copy.deepcopy(item["info"]) for item in self._saved.values()]
        return sorted(
            [*(session.info() for session in sessions), *saved],
            key=lambda item: item["created_at"],
            reverse=True,
        )

    def get(self, session_id: str) -> ManagedSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            saved = self._saved.get(session_id)
            if saved is None:
                raise SessionNotFoundError(session_id)
            if self._closed:
                raise RuntimeError("session manager is closed")
            info = saved["info"]
            payload = saved["payload"]
            workspace = self.workspaces.get(info["workspace_id"])
            session = self._build_session(
                session_id=session_id,
                mode=info["permission_mode"],
                workspace_id=info["workspace_id"],
                workspace=workspace,
                history=payload["messages"],
                created_at=info["created_at"],
                updated_at=info["updated_at"],
                restored=True,
                initial_status=(
                    "idle" if info["status"] == "dormant" else info["status"]
                ),
                last_error=info["last_error"],
                title=info.get("title", "New Session"),
                model=info.get("model", "unknown"),
                parent_session_id=info.get("parent_session_id"),
            )
            self._sessions[session_id] = session
            self._saved.pop(session_id, None)
            return session

    def start_run(
        self,
        session_id: str,
        message: str,
        *,
        attachments=(),
        allowed_tools=(),
        model: str | None = None,
    ) -> dict:
        return self.get(session_id).start_run(
            message,
            attachments=attachments,
            allowed_tools=allowed_tools,
            model=model,
        )

    def info(self, session_id: str) -> dict:
        session = self.get(session_id)
        result = session.info()
        result["children"] = [
            item["id"] for item in self.list()
            if item.get("parent_session_id") == session_id
        ]
        return result

    def rename(self, session_id: str, title: str) -> dict:
        session = self.get(session_id)
        session.rename(title)
        return self.info(session_id)

    def fork(self, session_id: str, turn_number: int | None = None) -> dict:
        from nz_coder.interface.timeline import fork_history, forked_session_title, conversation_turns
        from nz_coder.protocol.message_schema import rebind_fork_history

        parent = self.get(session_id)
        with parent._lock:
            if parent._run_thread is not None:
                raise SessionBusyError("cannot fork an active session run")
            turns = conversation_turns(parent.history)
            if not turns:
                raise ValueError("No user turns are available to fork")
            selected = int(turn_number) if turn_number is not None else turns[-1].number
            parent_history = copy.deepcopy(parent.history)
            title = forked_session_title(parent.title)
            mode = parent.permission_mode
            workspace_id = parent.workspace_id
            workspace = parent.workspace
            model = parent.model

        with self._lock:
            if self._closed:
                raise RuntimeError("session manager is closed")
            while True:
                new_id = create_session_id("fork")
                if new_id not in self._sessions and new_id not in self._saved:
                    break
            history = rebind_fork_history(fork_history(parent_history, selected), new_id)
            child = self._build_session(
                session_id=new_id,
                mode=mode,
                workspace_id=workspace_id,
                workspace=workspace,
                history=history,
                title=title,
                model=model,
                parent_session_id=session_id,
            )
            self._sessions[new_id] = child
            try:
                with scoped_workdir(workspace):
                    save_session(
                        child._persistence_messages(),
                        mode=mode,
                        session_id=new_id,
                        activate=False,
                        run_status="idle",
                        require_aliases=False,
                        title=title,
                        parent_session_id=session_id,
                        model=model,
                    )
            except Exception:
                self._sessions.pop(new_id, None)
                child.dispose(force=True)
                raise
        return self.info(new_id)

    def remove(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                saved = self._saved.get(session_id)
                if saved is not None:
                    workspace = self.workspaces.get(saved["info"]["workspace_id"])
                    with scoped_workdir(workspace):
                        delete_session(session_id)
                    self._saved.pop(session_id, None)
                    return True
                raise SessionNotFoundError(session_id)
        session.delete_persisted()
        with self._lock:
            self._sessions.pop(session_id, None)
        return True

    def list_workspaces(self) -> list[dict]:
        """Return the operator-authorized workspace roots."""
        return self.workspaces.list()

    def close(self, timeout: float = 5.0) -> None:
        close_timeout = _validated_wait_timeout(timeout, "close", allow_none=False)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.abort()
        deadline = time.monotonic() + close_timeout
        for session in sessions:
            session.wait(max(0.0, deadline - time.monotonic()))
            session.dispose(force=True)
    def _build_session(
        self,
        *,
        session_id: str,
        mode: str,
        workspace_id: str,
        workspace: Path,
        history: list[dict] | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
        restored: bool = False,
        initial_status: str = "idle",
        last_error: str = "",
        title: str = "New Session",
        model: str = "unknown",
        parent_session_id: str | None = None,
    ) -> ManagedSession:
        if self._agent_factory is None:
            agent = None
            client = AgentClient()
            with scoped_workdir(workspace):
                event_bus = SessionEventBus(
                    session_id=session_id,
                    journal_path=session_runtime_dir(session_id) / "events.jsonl",
                )
        else:
            with scoped_workdir(workspace):
                agent = self._agent_factory(session_id, mode)
            client = None
            event_bus = agent.event_bus
        if model == "unknown":
            if agent is not None:
                model = str(getattr(agent, "model_id", "unknown") or "unknown")
            else:
                try:
                    from nz_coder.providers.models import active_model_selection

                    model = str(active_model_selection(workspace).model_id or "unknown")
                except Exception:
                    model = "unknown"
        return ManagedSession(
            session_id,
            mode,
            agent,
            self._run_gates[workspace_id],
            self._interaction_timeout_seconds,
            workspace_id=workspace_id,
            workspace=workspace,
            history=history,
            created_at=created_at,
            updated_at=updated_at,
            restored=restored,
            initial_status=initial_status,
            last_error=last_error,
            title=title,
            model=model,
            parent_session_id=parent_session_id,
            client=client,
            event_bus=event_bus,
        )

    def _discover_saved(self, limit: int) -> None:
        ambiguous_ids: set[str] = set()
        for workspace_info in self.workspaces.list():
            workspace = Path(workspace_info["path"])
            with scoped_workdir(workspace):
                paths = list_sessions(limit=limit)
                for path in paths:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_size > _MAX_SAVED_SESSION_BYTES:
                        continue
                    payload = load_session(path.stem)
                    saved = self._validated_saved(
                        payload,
                        filename_id=path.stem,
                        workspace_id=workspace_info["id"],
                        workspace=workspace,
                        modified_at=stat.st_mtime,
                    )
                    if saved is None:
                        continue
                    session_id = saved["info"]["id"]
                    if session_id in ambiguous_ids:
                        continue
                    if session_id in self._saved:
                        # Session IDs are global API identities. Ambiguous copies
                        # are not exposed from either workspace.
                        self._saved.pop(session_id, None)
                        ambiguous_ids.add(session_id)
                        continue
                    self._saved[session_id] = saved

    @staticmethod
    def _validated_saved(
        payload: dict,
        *,
        filename_id: str,
        workspace_id: str,
        workspace: Path,
        modified_at: float,
    ) -> dict | None:
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("session_id")
        if (
            not isinstance(session_id, str)
            or session_id != filename_id
            or not _SAFE_SESSION_ID.fullmatch(session_id)
        ):
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            return None
        mode = payload.get("mode") or config.PERMISSION_MODE
        if mode not in MODES:
            return None
        persisted_workspace_text = payload.get("workspace")
        if not isinstance(persisted_workspace_text, str) or not persisted_workspace_text:
            return None
        try:
            persisted_workspace = Path(persisted_workspace_text).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if persisted_workspace != workspace:
            return None
        persisted_status = str(payload.get("run_status") or "dormant")
        if persisted_status not in {
            "cancelled",
            "completed",
            "completed_unverified",
            "dormant",
            "error",
            "failed",
            "idle",
            "interrupted",
            "max_turns",
            "running",
        }:
            persisted_status = "dormant"
        interrupted = persisted_status == "running"
        if interrupted:
            settle_interrupted_parts(messages)
        info = {
            "id": session_id,
            "status": "interrupted" if interrupted else persisted_status,
            "runtime_status": "interrupted" if interrupted else persisted_status,
            "permission_mode": mode,
            "workspace_id": workspace_id,
            "workspace": str(workspace),
            "restored": True,
            "message_count": len(messages),
            "created_at": modified_at,
            "updated_at": modified_at,
            "running": False,
            "pending_interaction_count": 0,
            "last_error": (
                "service stopped before the accepted run settled"
                if interrupted
                else ""
            ),
            "last_result": {},
            "title": str(payload.get("title") or "New Session"),
            "model": str(payload.get("model") or "unknown"),
            "parent_session_id": payload.get("parent_session_id"),
        }
        return {"info": info, "payload": copy.deepcopy(payload)}
