"""Terminal host adapter for the canonical product Agent runtime."""
from __future__ import annotations

import copy
import asyncio
from dataclasses import replace

from nz_coder.runtime.adapters.runner import run_request_from_legacy_host
from nz_coder.runtime.execution.native_sdk import NativeSDKRunner
from nz_coder.sdk import AgentClient


class TerminalSessionController:
    """Keep terminal presentation state outside the shared execution runtime."""

    def __init__(self, environment) -> None:
        self.environment = environment
        self._client = AgentClient(runner=NativeSDKRunner(environment))
        self._active_task = None

    def replace_environment(self, environment) -> None:
        """Retarget subsequent turns after a session/model command rebuild."""
        try:
            self.ensure_environment_replaceable()
        except BaseException:
            if environment is not self.environment:
                environment.close()
            raise
        self.environment = environment
        self._client = AgentClient(runner=NativeSDKRunner(environment))

    def ensure_environment_replaceable(self) -> None:
        from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
        if self.environment.has_pending_settlement:
            raise PublicRuntimeError(PublicError("session_commit_pending",
                "The previous result is waiting for Session storage.", True))
        if self._active_task is not None and not self._active_task.done():
            from nz_coder.runtime.session.ownership import session_busy
            raise session_busy()
        from nz_coder.runtime.session.store import require_history_confirmed
        from nz_coder.runtime.session.model import SessionIdentity
        require_history_confirmed(self.environment.runtime_services.session_runtime.store,
                                  SessionIdentity(self.session_id), self.environment.workdir)

    @property
    def session_id(self) -> str:
        return str(self.environment.session_id)

    @property
    def permissions(self):
        return self.environment.permissions

    @property
    def model_capabilities(self):
        return self.environment.model_capabilities

    def skills(self) -> list[dict]:
        loader = getattr(self.environment, "_skill_loader", None)
        return loader.list_skills() if loader is not None else []

    def mcp_status(self) -> list[dict]:
        runtime = getattr(self.environment, "_mcp_runtime", None)
        return runtime.status_summary() if runtime is not None else []

    def lsp_status(self) -> dict:
        from nz_coder.lsp.manager import client_status_summary
        return {'entries':client_status_summary(self.environment.workdir),
                'process_children':'owner_unavailable'}

    def lsp_control(self, entry_id: str, action: str) -> dict:
        from nz_coder.lsp.manager import control_client
        return control_client(self.environment.workdir,entry_id,action)

    def memory_report(self) -> str:
        manager = getattr(self.environment, "_mm", None)
        return manager.list_memories() if manager is not None else "No memories."

    def memory_control(self):
        """Return the Session-owned memory review control plane."""
        from nz_coder.state.memory_control import MemoryControlPlane

        manager = getattr(self.environment, "_mm", None)
        if manager is None:
            return None
        return MemoryControlPlane(manager, trusted_context={
            "capability": "manage", "scope_id": manager.scope_id, "session_id": self.session_id,
            "actor_id": "terminal-user", "operation_namespace": f"{manager.scope_id}:user:{self.session_id}",
        })

    def memory_manager(self):
        """Return the Session-owned manager for explicit user curation."""
        return getattr(self.environment, "_mm", None)

    def status_report(self, history: list[dict]) -> str:
        from nz_coder.state.workspace import status_report

        return status_report(self.environment, history)

    def trace_report(self) -> str:
        from nz_coder.state.trace import latest_trace, summarize_trace

        path = latest_trace(session_id=self.session_id) or latest_trace()
        return summarize_trace(path)

    async def compact(self, history: list[dict], focus: str | None = None) -> list[dict]:
        from nz_coder.foundation.async_utils import to_thread_settled
        from nz_coder.runtime.session.model import SessionIdentity
        self.ensure_environment_replaceable()
        runtime = self.environment.runtime_services.session_runtime
        identity = SessionIdentity(self.session_id)
        lease = runtime.store.acquire(identity, self.environment.workdir)
        self._active_task = asyncio.current_task()
        try:
            from nz_coder.foundation.workspace_trust import load_config_snapshot
            from nz_coder.providers.models import active_model_selection
            from nz_coder.runtime.execution.loop import resolve_model_runtime, ModelSelectionRequest
            from nz_coder.runtime.execution.run_control import RunControlBundle
            snapshot = load_config_snapshot(self.environment.workdir)
            selection = active_model_selection(self.environment.workdir, config_snapshot=snapshot)
            model_runtime = resolve_model_runtime(ModelSelectionRequest(
                provider_name=selection.provider, model_id=selection.model_id,
                variant=selection.variant, workspace=self.environment.workdir,
                config_snapshot=snapshot,
            ))
            # 显式摘要没有 Run；只借用已有资源凭据保留清理失败后的重试归属。
            resources = RunControlBundle(
                config_snapshot=snapshot, permissions=None, plan_mode=None,
                skill_loader=None, hooks=None, mcp_runtime=None, model_runtime=model_runtime,
                provider_runtimes={(model_runtime.provider_id, model_runtime.model_id): model_runtime},
                owns_provider_runtimes=True,
            )
            try:
                candidate = await to_thread_settled(self.environment._compact_messages,
                    copy.deepcopy(history), focus=focus, model_runtime=model_runtime,
                    cancel_callback=self.environment._cancel_compaction)
            finally:
                await to_thread_settled(self.environment._attempt_run_control_cleanup,
                                       resources, "explicit-compaction")
            return await runtime.replace_transcript(identity, self.environment.workdir,
                                                    history, candidate, session_lease=lease)
        finally:
            self._active_task = None
            lease.close_hold()

    def diff(self) -> str:
        from nz_coder.state.changes import render_latest_diff

        tracker = getattr(self.environment, "change_tracker", None)
        return tracker.render_diff() if tracker is not None else render_latest_diff()

    def processes(self) -> list[dict]:
        from nz_coder.runtime.process.process_service import workspace_process_service

        service = workspace_process_service(self.environment.workdir)
        values = []
        for handle in service.list(owner_session_id=self.session_id):
            item = handle.to_dict()
            result = service.read(
                handle.process_id,
                owner_session_id=self.session_id,
                cursor=-1,
                max_bytes=1,
            )
            item.update({
                "buffer_bytes": result.buffer_end_cursor - result.buffer_start_cursor,
                "pty_tier": "pty" if handle.tty else "pipe",
            })
            values.append(item)
        return values

    def process_read(
        self,
        process_id: str,
        *,
        cursor: int | None = None,
        tail_bytes: int | None = 8192,
        max_bytes: int = 8192,
        wait_seconds: float = 0.0,
    ) -> dict:
        from nz_coder.runtime.process.process_service import workspace_process_service

        return workspace_process_service(self.environment.workdir).read(
            process_id,
            owner_session_id=self.session_id,
            cursor=cursor,
            tail_bytes=tail_bytes,
            max_bytes=max_bytes,
            wait_seconds=wait_seconds,
        ).to_dict()

    def process_kill(self, process_id: str) -> dict:
        from nz_coder.runtime.process.process_service import workspace_process_service

        return workspace_process_service(self.environment.workdir).kill(
            process_id,
            owner_session_id=self.session_id,
        ).to_dict()

    def undo(self, history: list[dict]):
        return self._transition(history, undo=True)

    def redo(self, history: list[dict]):
        return self._transition(history, undo=False)

    def _transition(self, history: list[dict], *, undo: bool):
        from nz_coder.runtime.session.model import Session, SessionIdentity
        self.ensure_environment_replaceable()
        store = self.environment.runtime_services.session_runtime.store
        identity, workspace = SessionIdentity(self.session_id), self.environment.workdir
        lease = store.acquire(identity, workspace)
        try:
            session = store.load_session(identity, workspace)
            if session is None:
                session = Session.create(self.session_id, history, workspace=workspace)
            candidate = copy.deepcopy(session.transcript)
            write = store.prepare_history_transition(session)
            def commit(messages):
                session.replace_transcript(messages, allow_terminal=True)
                session.metadata.pop("_nz_terminal_result", None)
                session.metadata.pop("_nz_http_terminal_result", None)
                write.commit(session)
            reverter = self.environment.session_reverter
            result = (reverter.revert if undo else reverter.unrevert)(candidate, commit_history=commit)
            history[:] = candidate
            return result
        finally:
            lease.close_hold()

    def clear_scratchpad(self) -> None:
        self.environment.clear_scratchpad()

    def cancel(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def run(
        self,
        messages: list[dict],
        *,
        on_tool=None,
        on_text=None,
        on_token=None,
        stream: bool = True,
        allowed_tools: tuple[str, ...] | None = None,
        model: str | None = None,
        config_snapshot=None,
    ) -> dict:
        """Execute one terminal turn through AgentClient and sync its transcript."""
        # Injected embedders and CLI recovery tests may supply only the public
        # Agent facade. Product composition always supplies runtime_services.
        if not hasattr(self.environment, "runtime_services"):
            return await self.environment.run(
                messages,
                on_tool=on_tool,
                on_text=on_text,
                on_token=on_token,
                stream=stream,
            )
        from nz_coder.runtime.session.store import recover_history_transition
        from nz_coder.runtime.session.model import SessionIdentity
        from nz_coder.foundation.async_utils import to_thread_settled
        recovery = asyncio.create_task(to_thread_settled(
            recover_history_transition, self.environment.runtime_services.session_runtime.store,
            SessionIdentity(self.session_id), self.environment.workdir))
        cancelled = None
        while True:
            try:
                recovered = await asyncio.shield(recovery)
                break
            except asyncio.CancelledError as error:
                if recovery.cancelled():
                    raise
                cancelled = error
        if recovered is not None and recovered.resolution == "candidate":
            from nz_coder.runtime.session.store import adopt_history_transition
            adopt_history_transition(messages, recovered.original_history, recovered.candidate.transcript)
        # 确认已释放凭据后，必须先把候选交给UI并保留新输入，再传播取消。
        if cancelled is not None:
            raise cancelled
        settlement = await self._client.retry_settlement(self.environment.workdir, self.session_id)
        if settlement.state in {"pending", "conflict"}:
            from nz_coder.protocol.public_error import PublicRuntimeError
            raise PublicRuntimeError(settlement.error)
        if settlement.state == "committed":
            # CLI手中的旧检查点可能少了原终态；只把本次新提交接到确认后的原件。
            from nz_coder.protocol.message_schema import is_user_request_message
            from nz_coder.runtime.session.model import SessionIdentity
            from nz_coder.runtime.session.runtime import preview_activation_transcript
            start = next((i for i in range(len(messages) - 1, -1, -1)
                          if is_user_request_message(messages[i])), len(messages))
            durable = await self.environment.runtime_services.session_runtime.store.load(
                SessionIdentity(self.session_id), self.environment.workdir)
            messages[:] = preview_activation_transcript(durable, messages[start:])
        provider_override = None
        model_override = model
        from nz_coder.foundation.workspace_trust import load_config_snapshot
        from nz_coder.providers.models import active_model_selection

        run_snapshot = config_snapshot or load_config_snapshot(self.environment.workdir)
        selection = active_model_selection(
            self.environment.workdir, config_snapshot=run_snapshot,
        )
        if isinstance(model, str) and "/" in model:
            provider_override, model_override = model.split("/", 1)
        provider_override = provider_override or selection.provider
        model_override = model_override or selection.model_id
        request = run_request_from_legacy_host(
            self.environment,
            messages,
            stream,
            allowed_tools=allowed_tools,
            provider_override=provider_override,
            model_override=model_override,
        )
        request = replace(
            request,
            reasoning_effort=selection.variant,
            agent=replace(request.agent, reasoning_effort=selection.variant),
        )
        self._active_task = asyncio.current_task()
        try:
            result = await self._client.run(
                request,
                on_tool=on_tool,
                on_text=on_text,
                on_token=on_token,
                config_snapshot=run_snapshot,
            )
        finally:
            self._active_task = None
        messages[:] = copy.deepcopy(list(result.messages))
        return {
            "status": result.status.value,
            "last_error": result.error,
            "runtime": copy.deepcopy(result.metadata.get("runtime", {})),
            "verification": copy.deepcopy(result.metadata.get("verification", {})),
        }

    def close(self) -> None:
        """Close the current product environment exactly once."""
        from nz_coder.runtime.process.process_service import dispose_session_processes
        from nz_coder.runtime.process.workdir import current_workdir

        dispose_session_processes(current_workdir(), self.session_id)
        close = getattr(self.environment, "close", None)
        if callable(close):
            close()


__all__ = ["TerminalSessionController"]
