"""In-process session ownership for the optional local HTTP transport."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass
import hmac
import logging
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
    is_synthetic_user_message,
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
from nz_coder.runtime.session.store import _valid_persisted_parts
from nz_coder.sdk import AgentClient
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.protocol.public_error import public_error_message, public_error_from_wire, to_public_error
from nz_coder.state.sessions import (
    create_session_id,
    list_sessions,
    load_session,
    rename_session,
    save_session,
    session_snapshot_dir,
    session_runtime_dir,
    _read_saved_session,
    _file_identity,
    _exact_session_id,
    session_dir,
    SessionReadError,
)
from nz_coder.tool_platform.permissioning.modes import MODES

from .interactions import InteractionBroker
from .workspaces import WorkspaceRegistry

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
@dataclass(frozen=True)
class SessionCandidate:
    session_id: str
    workspace_id: str
    path: Path
    file_identity: tuple

_HTTP_TERMINAL_RESULT_KEY = "_nz_http_terminal_result"
_LOGGER = logging.getLogger(__name__)


def _valid_session_identity(session_id: str) -> bool:
    try:
        _exact_session_id(session_id)
        return True
    except ValueError:
        return False


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


def _saved_terminal_result(payload: dict, status: str) -> dict | None:
    """只恢复与最后一次真实请求、同次终态提交相符的结果。"""
    if status in {"running", "idle", "dormant"}:
        return None
    from nz_coder.runtime.core.result import saved_terminal_result
    canonical = saved_terminal_result(payload)
    if canonical is not None:
        return canonical
    metadata = payload.get("metadata")
    record = metadata.get(_HTTP_TERMINAL_RESULT_KEY) if isinstance(metadata, dict) else None
    if not isinstance(record, dict):
        return None
    run_id = record.get("interaction_run_id")
    result = record.get("last_result")
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or record.get("status") != status
        or not isinstance(record.get("last_error"), str)
        or not isinstance(result, dict)
    ):
        return None
    user = next(
        (message for message in reversed(payload["messages"])
         if message.get("role") == "user" and not is_synthetic_user_message(message)),
        None,
    )
    if user is None or user.get(INTERACTION_RUN_ID_KEY) != run_id:
        return None
    for key in ("status", "answer", "active_agent", "error", "last_error"):
        if key in result and not isinstance(result[key], str):
            return None
    if "metadata" in result:
        result_metadata = result["metadata"]
        if not isinstance(result_metadata, dict):
            return None
        if "interaction_run_id" in result_metadata and result_metadata["interaction_run_id"] != run_id:
            return None
    return copy.deepcopy(record)


def _canonical_terminal_record(payload: dict) -> dict:
    from nz_coder.runtime.core.result import saved_terminal_result
    record = saved_terminal_result(payload)
    if record is not None:
        return record
    # 旧快照没有正式投影时只显示已提交状态，不从全历史猜测本次答案。
    return {"interaction_run_id": payload["metadata"]["active_interaction_run_id"],
            "status": payload["run_status"], "last_error": "", "last_result": {}}


def build_http_agent(session_id: str, permission_mode: str):
    """Build an AgentLoop with a conservative pre-broker permission fallback."""
    from nz_coder.state.memory import (
        bind_memory_manager,
        workspace_memory_manager,
    )
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.execution.composition import build_coding_agent
    from nz_coder.runtime.process.workdir import current_workdir
    from nz_coder.state.memory_scope import session_memory_binding, scoped_memory_scope
    from nz_coder.state.skills import SkillLoader, bind_skill_loader, current_skill_loader
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    memory_scope, _capability = session_memory_binding(session_id, current_workdir())
    memory_manager = workspace_memory_manager(scope=memory_scope)
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
    # Project skill authority belongs to a top-level Run, not this long-lived
    # HTTP Session.  AgentLoop.prepare_run_control() appends the descriptions
    # captured from the same snapshot used by every other run control.
    system_prompt = build(memory_block="", skill_descriptions="")
    with bind_memory_manager(memory_manager), bind_skill_loader(skill_manager), scoped_memory_scope(memory_scope):
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
        last_result: dict | None = None,
        active_interaction_run_id: str = "",
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
        self.last_result: dict = copy.deepcopy(last_result) if last_result is not None else {}
        self._lock = threading.RLock()
        self._run_thread: threading.Thread | None = None
        self._run_loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task | None = None
        self._run_phase = "idle"
        self._run_event_floor = 0
        self._active_interaction_run_id = active_interaction_run_id
        self._active_event_publisher = None
        self._cancel_requested = False
        self._disposed = False
        self._closing = False
        self._dispose_lock = threading.Lock()
        self._dispose_completed: set[str] = set()
        self._run_gate = run_gate
        self._gate_acquired = False
        self._session_lease = None
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

    def _model_identity(self, config_snapshot=None) -> tuple[str, str, str | None]:
        if config_snapshot is not None:
            from nz_coder.providers.models import active_model_selection

            selected = active_model_selection(
                self.workspace, config_snapshot=config_snapshot,
            )
            return selected.provider, selected.model_id, selected.variant
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
        config_snapshot=None,
    ) -> RunRequest:
        from nz_coder.runtime.conversation.prompt import build

        provider, model, variant = self._model_identity(config_snapshot)
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
            return self._info_from_pending(self.interactions.list())

    def _info_from_pending(self, pending: list[dict]) -> dict:
        """调用方持会话锁，以同一份交互清单派生状态，避免快照回调反取审批锁。"""
        self._refresh_committed_result()
        runtime_status = self.status
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
        from nz_coder.runtime.agent.planning_runtime import invalidate_working_plan
        from nz_coder.state.sessions import session_runtime_state_path

        with scoped_workdir(self.workspace):
            snapshot_root = session_snapshot_dir(self.session_id)
            state_path = session_runtime_dir(self.session_id) / "message_revert.json"
            runtime_path = session_runtime_state_path(self.session_id)
        return SessionReverter(
            WorkspaceSnapshotStore(self.workspace, snapshot_root),
            state_path,
            before_transition=lambda: invalidate_working_plan(
                getattr(self.agent, "runtime_state", None), path=runtime_path),
        )

    def undo(self) -> dict:
        return self._transition_history(undo=True)

    def redo(self) -> dict:
        return self._transition_history(undo=False)

    def _transition_history(self, *, undo: bool) -> dict:
        from nz_coder.runtime.session.model import SessionIdentity
        from nz_coder.runtime.session.store import LegacyJsonSessionStore
        with self._lock:
            if self._run_thread is not None:
                raise SessionBusyError("cannot change history during an active session run")
            if not self._run_gate.acquire(blocking=False):
                raise SessionBusyError("another session already has an active run in this workspace")
            lease = None
            try:
                store = LegacyJsonSessionStore()
                identity = SessionIdentity(self.session_id, self.parent_session_id)
                lease = store.acquire(identity, self.workspace)
                session = store.load_session(identity, self.workspace)
                if session is None:
                    raise SessionNotFoundError(self.session_id)
                candidate = copy.deepcopy(session.transcript)
                write = store.prepare_history_transition(session)
                def commit(messages):
                    session.replace_transcript(messages, allow_terminal=True)
                    session.metadata.pop("_nz_terminal_result", None)
                    session.metadata.pop(_HTTP_TERMINAL_RESULT_KEY, None)
                    write.commit(session)
                reverter = self._reverter()
                result = (reverter.revert if undo else reverter.unrevert)(candidate, commit_history=commit)
                self.history = candidate
                self.last_result = {}
                self.last_error = ""
                self.updated_at = time.time()
                return {"message_id": result.message_id, "files": list(result.files),
                        "removed_messages": result.removed_messages}
            finally:
                if lease is not None:
                    lease.close_hold()
                self._run_gate.release()

    def _result_metadata(self, record: dict | None = None) -> dict:
        """在同一工作区保留 Native 元数据，只更新管理器专属结果。"""
        with scoped_workdir(self.workspace):
            metadata = load_session(self.session_id).get("metadata")
        metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        metadata.pop(_HTTP_TERMINAL_RESULT_KEY, None)
        if record is not None:
            metadata[_HTTP_TERMINAL_RESULT_KEY] = copy.deepcopy(record)
        return metadata

    def messages(self) -> list[dict]:
        with self._lock:
            self._refresh_committed_result()
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

            with self.interactions.pending_snapshot() as pending:
                info = self._info_from_pending(pending)

                def capture() -> dict:
                    return {
                        "schema_version": MESSAGE_SCHEMA_VERSION,
                        "snapshot_id": snapshot_id,
                        "session": copy.deepcopy(info),
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
            # 与交互发布保持同一锁序；清单冻结到游标生成，不能靠有限重放修补。
            with self.interactions.pending_snapshot() as pending_items:
                pending = {
                    "permissions": [item for item in pending_items if item["kind"] == "permission"],
                    "questions": [item for item in pending_items if item["kind"] == "question"],
                }
                info = self._info_from_pending(pending_items)

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
                        "session": copy.deepcopy(info),
                        "summary": session_summary(self.history),
                        "messages": timeline_messages,
                        "active_interaction_run_id": self._active_interaction_run_id,
                        "run": {
                            "interaction_run_id": self._active_interaction_run_id,
                            "status": info["runtime_status"],
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

        from nz_coder.foundation.workspace_trust import load_config_snapshot

        snapshot = load_config_snapshot(self.workspace)
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
                config_snapshot=snapshot,
            ).list()
        ]

    def extensions(self) -> list[dict]:
        """Project extension owners from this Session's daemon workspace."""
        from nz_coder.extensions.registry import ExtensionRegistry
        from nz_coder.state.skills import SkillLoader

        from nz_coder.foundation.workspace_trust import load_config_snapshot

        snapshot = load_config_snapshot(self.workspace)
        loader = SkillLoader(
            project_dir=self.workspace / ".nz-coder" / "skills",
            workspace_trusted=snapshot.control_plane_trusted,
            project_control_snapshot=snapshot.project_control,
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
        from nz_coder.foundation.workspace_trust import (
            load_config_snapshot,
            scoped_config_snapshot,
        )

        manager = self._workflow_manager()
        snapshot = load_config_snapshot(self.workspace)
        with scoped_config_snapshot(snapshot):
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
        return manager, resolved, summary, workflow_approval_digest(summary), snapshot

    def prepare_workflow(self, name: str, arguments: dict) -> dict:
        """Resolve and fingerprint one exact data-only Workflow plan."""
        _manager, resolved, summary, approval_digest, _snapshot = (
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

        manager, resolved, _summary, expected, snapshot = self._resolve_workflow_for_approval(
            name, arguments
        )
        provided = str(approval_digest or "")
        if not provided or not hmac.compare_digest(provided, expected):
            raise ValueError("workflow approval is stale or does not match this plan")
        # WorkflowHostSDK copies the current context into its background
        # thread. Bind the exact snapshot that produced the approved plan so
        # nested workflow resolution cannot observe a later disk generation.
        from nz_coder.foundation.workspace_trust import scoped_config_snapshot

        with scoped_config_snapshot(snapshot):
            handle = WorkflowHostSDK(manager).start(
                plan=resolved["capsule"]["plan"],
                display_name=str(
                    resolved["capsule"]["manifest"].get("name") or name
                ),
                approval_decision="approve",
                approval_digest=provided,
            )
        return handle.wait_started(10.0)

    def memory_manager(self):
        manager = getattr(self.agent, "_mm", None)
        if manager is not None:
            return manager
        from nz_coder.state.memory import workspace_memory_manager
        from nz_coder.state.memory_scope import MemoryScope

        # 原生会话尚未开始模型运行时也能管理记忆；身份只来自宿主持久资料。
        with scoped_workdir(self.workspace):
            metadata = load_session(self.session_id).get("metadata") or {}
            value = metadata.get("memory_scope")
            scope = MemoryScope.from_dict(value) if value is not None else MemoryScope.for_workspace(self.workspace)
            return workspace_memory_manager(scope=scope)

    def _memory_context(self, operation_id=None):
        manager = self.memory_manager()
        context = {"capability": "manage", "scope_id": manager.scope_id,
                   "session_id": self.session_id, "actor_id": "remote-user",
                   "operation_namespace": f"{manager.scope_id}:user:{self.session_id}"}
        if operation_id is not None:
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise ValueError("operation_id must be a nonempty string")
            context["operation_id"] = operation_id
        return context

    def memory_control(self):
        from nz_coder.state.memory_control import MemoryControlPlane
        return MemoryControlPlane(self.memory_manager(), trusted_context=self._memory_context())

    def lsp_status(self) -> dict:
        from nz_coder.lsp.manager import client_status_summary
        return {'entries':client_status_summary(self.workspace),
                'process_children':'owner_unavailable'}

    def lsp_control(self, entry_id: str, action: str) -> dict:
        from nz_coder.lsp.manager import control_client
        return control_client(self.workspace,entry_id,action)

    def memory_status(self) -> dict:
        control = self.memory_control()
        return {"pending": control.pending(), "ledger": control.ledger(),
                "generation": self.memory_manager().generation()}

    def memory_records(self, *, type=None, status="active", limit=50, cursor=None) -> dict:
        if status not in {"active", "archived", "all"}:
            raise ValueError("memory status must be active, archived, or all")
        return self.memory_manager().list_records(type=type, status=None if status == "all" else status,
                                                  limit=limit, cursor=cursor)

    def memory_search(self, query: str, *, limit=5) -> dict:
        from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings

        # HTTP工作线程使用目标会话快照，避免借用其他请求或全局的检索设置。
        with scoped_run_settings(RunSettings.from_snapshot(self.config_snapshot)):
            return self.memory_manager().search_result(query, limit=limit)

    def memory_record(self, memory_id: str, *, revision=None) -> dict:
        record = self.memory_manager().read(memory_id, revision=revision)
        return record if record is not None else {"status": "not_found", "memory_id": memory_id}

    def memory_history(self, memory_id: str) -> list:
        return self.memory_manager().history(memory_id)

    def mutate_memory(self, command: dict, *, operation_id: str) -> dict:
        return self.memory_manager().mutate(command, trusted_context=self._memory_context(operation_id))

    def memory_proposal(self, proposal_id: str) -> dict:
        proposal = self.memory_control().get(proposal_id)
        return proposal if proposal is not None else {"status": "not_found", "proposal_id": proposal_id}

    def review_memory(self, proposal_id: str, action: str, *, expected_proposal_revision: int,
                      operation_id: str, expected_revision=None, related_records=None, reason="") -> dict:
        if not isinstance(expected_proposal_revision, int) or isinstance(expected_proposal_revision, bool) or expected_proposal_revision < 1:
            raise ValueError("expected_proposal_revision must be a positive integer")
        self._memory_context(operation_id)
        control = self.memory_control()
        arguments = {"expected_proposal_revision": expected_proposal_revision,
                     "operation_id": operation_id, "reviewer": "remote-user"}
        if action == "approve":
            return control.approve(proposal_id, expected_revision=expected_revision,
                                   related_records=related_records, **arguments)
        if action == "reject":
            return control.reject(proposal_id, reason=reason or "rejected by remote user", **arguments)
        raise ValueError("memory action must be approve or reject")

    def expand_command(self, name: str, arguments: str = "") -> dict:
        from nz_coder.interface.custom_commands import default_command_catalog

        if not isinstance(arguments, str):
            raise ValueError("command arguments must be a string")
        from nz_coder.foundation.workspace_trust import load_config_snapshot

        snapshot = load_config_snapshot(self.workspace)
        try:
            expanded = default_command_catalog(
                self.workspace,
                config_snapshot=snapshot,
            ).expand(name, arguments)
        except KeyError as exc:
            raise ValueError(f"custom command was not found: {name}") from exc
        result = {
            "name": expanded.name,
            "prompt": expanded.prompt,
            "source": expanded.source,
            "allowed_tools": list(expanded.allowed_tools),
            "model": expanded.model,
        }
        result["command_digest"] = self._command_execution_digest(
            snapshot,
            result["prompt"],
            result["allowed_tools"],
            result["model"],
        )
        return result

    @staticmethod
    def _command_execution_digest(
        snapshot, prompt: str, allowed_tools, model: str | None,
    ) -> str:
        """Bind a remote command expansion to one control fingerprint."""
        import hashlib
        import json

        payload = json.dumps({
            "control_fingerprint": snapshot.control_fingerprint,
            "prompt": str(prompt),
            "allowed_tools": [str(name) for name in allowed_tools],
            "model": str(model) if model is not None else None,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def start_run(
        self,
        message: str,
        *,
        attachments=(),
        allowed_tools=(),
        model: str | None = None,
        command_digest: str | None = None,
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
        provided_command_digest = str(command_digest or "").strip()
        if command_digest is not None and len(provided_command_digest) != 64:
            raise ValueError("command digest is invalid")
        from nz_coder.foundation.workspace_trust import load_config_snapshot

        self._retry_client_cleanup()
        run_snapshot = load_config_snapshot(self.workspace)
        if provided_command_digest:
            expected_command_digest = self._command_execution_digest(
                run_snapshot,
                message,
                selected_tools,
                model_override,
            )
            if not hmac.compare_digest(
                provided_command_digest, expected_command_digest,
            ):
                raise ValueError(
                    "custom command expansion is stale; expand it again"
                )
        with self._lock:
            if self._disposed or self._closing:
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
                self._settle_previous_result()
                from nz_coder.runtime.session.model import SessionIdentity
                from nz_coder.runtime.session.store import LegacyJsonSessionStore

                self._session_lease = LegacyJsonSessionStore().acquire(
                    SessionIdentity(self.session_id, self.parent_session_id),
                    self.workspace, outer_hold=True,
                )
                # 缓存可能早于另一客户端已完成的运行，须在获得写权后重读原件。
                with scoped_workdir(self.workspace):
                    canonical = load_session(self.session_id)
                if canonical:
                    self.history = copy.deepcopy(canonical['messages'])
                previous_history = copy.deepcopy(self.history)
                self.config_snapshot = run_snapshot
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
                provider_id, model_id, variant = self._model_identity(run_snapshot)
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
                from nz_coder.runtime.adapters.verification_runtime import preflight_runtime_restore
                from nz_coder.runtime.core.run_settings import RunSettings
                from nz_coder.runtime.core.execution_context import command_activation
                activation = command_activation([*self.history, user_message])
                preflight_runtime_restore(session_id=self.session_id, workspace=self.workspace,
                    activation=activation, settings=RunSettings.from_snapshot(run_snapshot))
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
                        session_metadata=self._result_metadata(),
                    )
                self.status = "running"
                self.last_error = ""
                self.last_result = {}
                self.updated_at = time.time()
                self._cancel_requested = False
                self._run_phase = "starting"
                run_request = (
                    self._run_request(
                        run_messages, selected_tools, model_override, run_snapshot,
                    )
                    if self.client is not None else None
                )
                thread = threading.Thread(
                    target=self._run_agent,
                    args=(
                        run_messages, selected_tools, model_override,
                        run_snapshot, run_request,
                    ),
                    name=f"nz-http-{self.session_id}",
                    daemon=True,
                )
                self._run_thread = thread
                thread.start()
            except Exception as exc:
                from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
                self._run_thread = None
                self._run_phase = "idle"
                self._run_event_floor = 0
                self.status = "failed"
                self.last_error = to_public_error(exc).message
                self.last_result = {}
                self.history = previous_history
                if (self._session_lease is not None
                        and not isinstance(exc, CheckpointRestoreError)
                        and not self._client_settlement_pending()):
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
                                session_metadata=self._result_metadata(),
                            )
                    except Exception:
                        pass
                self.interactions.cancel_all("start_failed", block_new=True)
                self._close_session_hold()
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

    def _request_close(self) -> None:
        with self._lock:
            self._closing = True
        runner = getattr(self.client, "_native_runner", None)
        notify = getattr(runner, "request_retirement", None)
        if callable(notify):
            notify(self.workspace, self.session_id)
        self.abort()

    def _client_settlement_pending(self) -> bool:
        if self.client is None:
            return bool(getattr(self.agent, "has_pending_settlement", False))
        runner = getattr(self.client, "_native_runner", None)
        pending = getattr(runner, "_has_pending_settlement", None)
        return bool(pending(self.workspace, self.session_id)) if callable(pending) else False

    def _settle_previous_result(self) -> None:
        if self._client_settlement_pending():
            operation = (self.client.retry_settlement(self.workspace, self.session_id)
                         if self.client is not None else self.agent.retry_settlement())
            result = asyncio.run(operation)
            if result.state in {"pending", "conflict"}:
                from nz_coder.protocol.public_error import PublicRuntimeError
                raise PublicRuntimeError(result.error)
        self._refresh_committed_result()

    def _refresh_committed_result(self) -> None:
        # 只在失败缓存与已确认原件之间重建显示；运行中和待结算时不读写竞争。
        if (self._run_thread is not None or self.status != "failed"
                or self._client_settlement_pending()):
            return
        with scoped_workdir(self.workspace):
            payload = load_session(self.session_id)
        from nz_coder.runtime.session.store import confirms_terminal_commit
        if not confirms_terminal_commit(payload, self.workspace, self.session_id, self._active_interaction_run_id):
            return
        record = _canonical_terminal_record(payload)
        self.history = copy.deepcopy(payload["messages"])
        self.status = record["status"]
        self.last_result, self.last_error = record["last_result"], record["last_error"]
        self.updated_at = time.time()

    def _retry_client_cleanup(self) -> None:
        runner = getattr(self.client, "_native_runner", None)
        retry = getattr(runner, "retry_cleanup", None)
        if callable(retry) and not retry(self.workspace, self.session_id):
            raise SessionBusyError("agent resource cleanup is incomplete")

    def _close_agent(self) -> None:
        if self.agent is None:
            self._retry_client_cleanup()
            self.event_bus.close()
            return
        self.agent.close()
        # Agent.close 会记录阶段失败而不抛错，必须读取其实际清理状态。
        if getattr(self.agent, "environment_cleanup_complete", True) is False:
            raise SessionBusyError("agent resource cleanup is incomplete")

    def dispose(self, *, force: bool = False, deadline: float | None = None) -> None:
        if force:
            self._request_close()
        from nz_coder.runtime.process.process_service import dispose_session_processes

        with self._dispose_lock:
            with self._lock:
                if self._disposed:
                    return
                if self._run_thread is not None:
                    raise SessionBusyError("wait for the active run before disposing the session")
                self._closing = True
            stages = (
                ("interactions", self.interactions.close),
                (
                    "processes",
                    lambda: dispose_session_processes(
                        self.workspace,
                        self.session_id,
                        deadline=deadline,
                    ),
                ),
                ("agent", self._close_agent),
            )
            failures = []
            for label, close in stages:
                if label in self._dispose_completed:
                    continue
                try:
                    close()
                except Exception as exc:
                    failures.append(exc)
                else:
                    self._dispose_completed.add(label)
            if failures:
                raise failures[0]
            with self._lock:
                self._disposed = True
                self.status = "disposed"
                self.updated_at = time.time()

    def delete_persisted(self) -> bool:
        """Atomically reject active runs, delete owned state, then dispose."""
        with self._lock:
            if self._disposed:
                return False
            if self._run_thread is not None:
                raise SessionBusyError("abort the active run before deleting the session")
            self._closing = True
            with scoped_workdir(self.workspace):
                deleted = delete_session(self.session_id)
            try:
                self.event_bus.publish(
                    "session.deleted",
                    {"session_id": self.session_id, "info": self.info()},
                )
            except RuntimeError:
                pass
        self.dispose()
        return deleted

    def _run_agent(
        self,
        run_messages: list[dict],
        allowed_tools: tuple[str, ...] = (),
        model_override: str | None = None,
        run_snapshot=None,
        run_request: RunRequest | None = None,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from nz_coder.foundation.workspace_trust import scoped_config_snapshot

        snapshot = run_snapshot or self.config_snapshot
        try:
            with scoped_workdir(self.workspace), scoped_config_snapshot(snapshot):
                if self.client is None:
                    # The legacy Agent adapter must not infer an interaction identity
                    # from the session-long EventBus. Bind the request-scoped identity
                    # explicitly for this invocation only.
                    self.agent._requested_interaction_run_id = (
                        self._active_interaction_run_id
                    )
                    self.agent._requested_session_lease = self._session_lease
                    run_kwargs = {"stream": True}
                    if callable(getattr(self.agent, "prepare_run_control", None)):
                        run_kwargs["config_snapshot"] = snapshot
                    task = loop.create_task(self.agent.run(run_messages, **run_kwargs))
                else:
                    task = loop.create_task(self.client.run(
                        run_request or self._run_request(
                            run_messages, allowed_tools, model_override, snapshot,
                        ),
                        permission_asker=self.interactions.ask_permission,
                        question_asker=self.interactions.ask_question,
                        workflow_approval_asker=lambda _summary: "reject",
                        event_bus=self.event_bus,
                        config_snapshot=snapshot,
                        session_lease=self._session_lease,
                    ))
            with self._lock:
                self._run_loop = loop
                self._run_task = task
                self._run_phase = "running"
                cancel_requested = self._cancel_requested
            if cancel_requested:
                task.cancel()

        except BaseException as exc:
            # 线程已启动但协程尚未进入 Runner，未 claim 的外层写权仍须归还。
            loop.close()
            with self._lock:
                self.status = "failed"
                self.last_error = to_public_error(exc).message
                self._run_loop = self._run_task = self._run_thread = None
                self._run_phase = "idle"
                self.interactions.cancel_all("start_failed", block_new=True)
                self._close_session_hold()
                self._release_run_gate()
            return

        status = "completed"
        error = ""
        result: dict = {}
        persisted = False
        preserve_canonical = False
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
                    "usage": asdict(value.usage),
                }
            else:
                result = copy.deepcopy(value) if isinstance(value, dict) else {"result": value}
                status = str(result.get("status") or "completed")
                if status in {"error", "failed", "blocked", "aborted"}:
                    for key in ("error", "last_error"):
                        if result.get(key):
                            result[key] = public_error_message(result[key])
            if status in {"error", "failed", "aborted"}:
                metadata = result.get("metadata") or {}
                public = public_error_from_wire(metadata.get("public_error"))
                if public is None and self.client is None:
                    # 旧宿主返回值在上面已投影，不能把投影后的普通字符串再次信任。
                    raw_result = value if isinstance(value, dict) else {}
                    public = (public_error_from_wire(raw_result.get("public_error"))
                              or public_error_from_wire(raw_result.get("error"))
                              or public_error_from_wire(raw_result.get("last_error")))
                if public is None:
                    public = to_public_error(None)
                error = public.message
                result["error"] = error
                result.setdefault("metadata", {})["public_error"] = public.to_dict()
                if "last_error" in result:
                    result["last_error"] = error
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
            preserve_canonical = isinstance(exc, CheckpointRestoreError) or self._client_settlement_pending()
            status = "failed"
            error = to_public_error(exc).message
            if self._client_settlement_pending():
                error = "The original result is waiting for Session storage."
        finally:
            if self.client is None:
                self.agent._requested_interaction_run_id = None
                self.agent._requested_session_lease = None
            if self.client is not None or callable(getattr(self.agent, "prepare_run_control", None)):
                # SDK可能已按冻结结果提交后仍传播原异常；宿主不能另写一份取消/失败。
                if self._client_settlement_pending():
                    preserve_canonical = True
                    status = "failed"
                    error = "The original result is waiting for Session storage."
                else:
                    from nz_coder.runtime.session.store import confirms_terminal_commit
                    try:
                        with scoped_workdir(self.workspace):
                            canonical = load_session(self.session_id)
                    except (OSError, ValueError):
                        preserve_canonical = True
                    else:
                        if confirms_terminal_commit(canonical, self.workspace, self.session_id, self._active_interaction_run_id):
                            record = _canonical_terminal_record(canonical)
                            run_messages[:] = copy.deepcopy(canonical["messages"])
                            late = {key: copy.deepcopy(result.get("metadata", {})[key])
                                    for key in ("changed_files", "environment_cleanup_complete", "environment_cleanup_failures")
                                    if key in result.get("metadata", {})
                                    and result.get("metadata", {}).get("interaction_run_id") == self._active_interaction_run_id}
                            status, error, result = record["status"], record["last_error"], record["last_result"]
                            if result:
                                result["metadata"].update(late)
                            preserve_canonical = True
                            persisted = True
            with self._lock:
                self._run_phase = "committing"
                if self._cancel_requested and not preserve_canonical:
                    status = "cancelled"
                if status == "cancelled":
                    # 沿用已接受的取消结果，不能同时发布另一份 error 状态和原因。
                    error = ""
                    result["status"] = status
                    result["error"] = ""
                    result.pop("public_error", None)
                    if "last_error" in result:
                        result["last_error"] = ""
                    metadata = result.get("metadata")
                    if isinstance(metadata, dict):
                        metadata.pop("public_error", None)
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
                persisted_messages = self._persistence_messages()
                terminal_record = {
                    "interaction_run_id": self._active_interaction_run_id,
                    "status": status,
                    "last_error": error,
                    "last_result": copy.deepcopy(result),
                }
            try:
                if not preserve_canonical:
                    with scoped_workdir(self.workspace):
                        save_session(
                            persisted_messages,
                            mode=self.permission_mode,
                            session_id=self.session_id,
                            activate=False,
                            run_status=status,
                            require_aliases=False,
                            model=self.model,
                            session_metadata=self._result_metadata(terminal_record),
                        )
                    persisted = True
                    try:
                        with scoped_workdir(self.workspace):
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
                    self._close_session_hold()
                    self._release_run_gate()
                    try:
                        (self._active_event_publisher or self.event_bus).publish(
                            "session.run.settled",
                            {"status": status, "persisted": persisted},
                        )
                    except RuntimeError:
                        pass

    def _close_session_hold(self) -> None:
        lease, self._session_lease = self._session_lease, None
        if lease is not None:
            lease.close_hold()

    def _release_run_gate(self) -> None:
        if not self._gate_acquired:
            return
        self._gate_acquired = False
        self._run_gate.release()


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
        self._saved: list[SessionCandidate] = []
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
                if session_id not in self._sessions and not self._find_candidates(session_id):
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

    @staticmethod
    def _summary(info: dict) -> dict:
        keys = ("id", "workspace_id", "title", "status", "runtime_status", "parent_session_id", "created_at", "updated_at", "message_count", "running")
        summary = {key: info.get(key) for key in keys}
        title = str(summary["title"] or "New Session")
        summary["title"] = title if len(title) <= 160 else title[:157] + "..."
        return summary

    def _find_candidates(self, session_id: str) -> list[SessionCandidate]:
        try:
            _exact_session_id(session_id)
        except ValueError:
            return []
        found = []
        for item in self.workspaces.list():
            with scoped_workdir(Path(item["path"])):
                path = session_dir() / f"{session_id}.json"
            try:
                identity = _file_identity(path.stat(follow_symlinks=False))
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SessionReadError("unreadable", path) from exc
            found.append(SessionCandidate(session_id, item["id"], path, identity))
        return found

    def _read_candidate(self, candidate: SessionCandidate) -> dict:
        workspace = self.workspaces.get(candidate.workspace_id)
        with scoped_workdir(workspace):
            payload = _read_saved_session(candidate.path, expected_id=candidate.session_id, expected_identity=candidate.file_identity)
        if payload is None:
            raise SessionReadError("changed", candidate.path)
        saved = self._validated_saved(payload, filename_id=candidate.session_id, workspace_id=candidate.workspace_id, workspace=workspace, modified_at=candidate.file_identity[3] / 1_000_000_000)
        if saved is None:
            raise SessionReadError("corrupt", candidate.path)
        parent = saved["info"].get("parent_session_id")
        if parent is not None and parent != "":
            try:
                if not isinstance(parent, str):
                    raise ValueError()
                _exact_session_id(parent)
            except (ValueError, TypeError):
                raise SessionReadError("corrupt", candidate.path)
        if parent == "":
            saved["info"]["parent_session_id"] = None
        return saved

    def _resolve_candidate(self, session_id: str) -> SessionCandidate:
        candidates = self._find_candidates(session_id)
        if not candidates:
            raise SessionNotFoundError(session_id)
        if len(candidates) > 1:
            raise SessionReadError("ambiguous", candidates[0].path)
        return candidates[0]

    def list(self) -> dict:
        with self._lock:
            live = dict(self._sessions)
            candidates = list(self._saved)
        summaries = [self._summary(session.info()) for session in live.values()]
        unavailable = []
        for candidate in candidates:
            if candidate.session_id in live:
                continue
            try:
                matches = self._find_candidates(candidate.session_id)
                if len(matches) > 1:
                    raise SessionReadError("ambiguous", candidate.path)
                saved = self._read_candidate(candidate)
                summaries.append(self._summary(saved["info"]))
            except SessionReadError as exc:
                _LOGGER.warning("Cannot discover Session %s: %s", candidate.session_id, exc.kind)
                unavailable.append({"candidate_id": candidate.session_id, "workspace_id": candidate.workspace_id, "code": exc.kind, "size_bytes": exc.size_bytes, "limit_bytes": exc.limit_bytes})
        summaries.sort(key=lambda item: item["created_at"], reverse=True)
        return {"sessions": summaries, "unavailable": unavailable}

    def get(self, session_id: str) -> ManagedSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            if self._closed:
                raise RuntimeError("session manager is closed")
            candidate = self._resolve_candidate(session_id)
            saved = self._read_candidate(candidate)
            info, payload = saved["info"], saved["payload"]
            workspace = self.workspaces.get(info["workspace_id"])
            session = self._build_session(
                session_id=session_id, mode=info["permission_mode"], workspace_id=info["workspace_id"], workspace=workspace,
                history=payload["messages"], created_at=info["created_at"], updated_at=info["updated_at"], restored=True,
                initial_status="idle" if info["status"] == "dormant" else info["status"], last_error=info["last_error"],
                last_result=info["last_result"], active_interaction_run_id=info["active_interaction_run_id"],
                title=info.get("title", "New Session"), model=info.get("model", "unknown"), parent_session_id=info.get("parent_session_id"))
            self._sessions[session_id] = session
            self._saved = [item for item in self._saved if item.session_id != session_id]
            return session

    def start_run(
        self,
        session_id: str,
        message: str,
        *,
        attachments=(),
        allowed_tools=(),
        model: str | None = None,
        command_digest: str | None = None,
    ) -> dict:
        return self.get(session_id).start_run(
            message,
            attachments=attachments,
            allowed_tools=allowed_tools,
            model=model,
            command_digest=command_digest,
        )

    def info(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                result = session.info()
            else:
                result = self._read_candidate(self._resolve_candidate(session_id))["info"]
                from nz_coder.providers.models import active_model_selection
                selected = active_model_selection(Path(result["workspace"]))
                result.update(mode=result["permission_mode"], provider=selected.provider,
                              reasoning_effort=selected.variant)
        result["children"] = [item["id"] for item in self.list()["sessions"] if item.get("parent_session_id") == session_id]
        return result

    def rename(self, session_id: str, title: str) -> dict:
        session = self.get(session_id)
        session.rename(title)
        return self.info(session_id)

    def fork(self, session_id: str, turn_number: int | None = None) -> dict:
        from nz_coder.interface.timeline import fork_history, forked_session_title, conversation_turns
        from nz_coder.protocol.message_schema import rebind_fork_history
        from nz_coder.runtime.session.session_revert import copy_fork_snapshots

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
                if new_id not in self._sessions and not self._find_candidates(new_id):
                    break
            history = rebind_fork_history(fork_history(parent_history, selected), new_id)
            copy_fork_snapshots(workspace, session_id, new_id, history)
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
                candidate = self._resolve_candidate(session_id)
                self._read_candidate(candidate)
                workspace = self.workspaces.get(candidate.workspace_id)
                with scoped_workdir(workspace):
                    delete_session(session_id)
                self._saved = [item for item in self._saved if item != candidate]
                return True
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
            if self._closed and not self._sessions:
                return
            self._closed = True
            sessions = list(self._sessions.values())
        failures: dict[str, Exception] = {}
        for session in sessions:
            try:
                session._request_close()
            except Exception as exc:
                failures[session.session_id] = exc
        deadline = time.monotonic() + close_timeout
        for session in sessions:
            try:
                if not session.wait(max(0.0, deadline - time.monotonic())):
                    raise SessionBusyError("session execution has not settled")
                session.dispose(deadline=deadline)
            except Exception as exc:
                failures[session.session_id] = exc
            else:
                # 只有实际清理成功才移除登记，未完成对象留给下一次关闭重试。
                with self._lock:
                    if self._sessions.get(session.session_id) is session:
                        self._sessions.pop(session.session_id)
                failures.pop(session.session_id, None)
        if failures:
            raise SessionBusyError(
                "session cleanup incomplete: " + ", ".join(failures)
            ) from next(iter(failures.values()))

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
        last_result: dict | None = None,
        active_interaction_run_id: str = "",
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
            last_result=last_result,
            active_interaction_run_id=active_interaction_run_id,
            title=title,
            model=model,
            parent_session_id=parent_session_id,
            client=client,
            event_bus=event_bus,
        )

    def _discover_saved(self, limit: int) -> None:
        for workspace_info in self.workspaces.list():
            workspace = Path(workspace_info["path"])
            with scoped_workdir(workspace):
                for path in list_sessions(limit=limit):
                    try:
                        identity = _file_identity(path.stat(follow_symlinks=False))
                    except OSError:
                        continue
                    self._saved.append(SessionCandidate(path.stem, workspace_info["id"], path, identity))

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
            or not _valid_session_identity(session_id)
        ):
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            return None
        if any(not _valid_persisted_parts(message) for message in messages):
            return None
        mode = payload.get("mode") or config.PERMISSION_MODE
        if not isinstance(mode, str) or mode not in MODES:
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
            "blocked",
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
        terminal_record = _saved_terminal_result(payload, persisted_status)
        if terminal_record is None:
            from nz_coder.runtime.session.store import confirms_terminal_commit
            interaction = (payload.get("metadata") or {}).get("active_interaction_run_id", "")
            if confirms_terminal_commit(payload, workspace, session_id, interaction):
                terminal_record = _canonical_terminal_record(payload)
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
                else terminal_record["last_error"] if terminal_record is not None else ""
            ),
            "last_result": terminal_record["last_result"] if terminal_record is not None else {},
            "active_interaction_run_id": (
                terminal_record["interaction_run_id"] if terminal_record is not None else ""
            ),
            "title": str(payload.get("title") or "New Session"),
            "model": str(payload.get("model") or "unknown"),
            "parent_session_id": payload.get("parent_session_id"),
        }
        return {"info": info, "payload": payload}
