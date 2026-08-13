"""Terminal host adapter for the canonical product Agent runtime."""
from __future__ import annotations

import copy
import asyncio

from nz_coder.runtime.adapters.runner import run_request_from_legacy_host
from nz_coder.runtime.native_sdk import NativeSDKRunner
from nz_coder.sdk import AgentClient


class TerminalSessionController:
    """Keep terminal presentation state outside the shared execution runtime."""

    def __init__(self, environment) -> None:
        self.environment = environment
        self._client = AgentClient(runner=NativeSDKRunner(environment))
        self._active_task = None

    def replace_environment(self, environment) -> None:
        """Retarget subsequent turns after a session/model command rebuild."""
        self.environment = environment
        self._client = AgentClient(runner=NativeSDKRunner(environment))

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

    def memory_report(self) -> str:
        manager = getattr(self.environment, "_mm", None)
        return manager.list_memories() if manager is not None else "No memories."

    def memory_control(self):
        """Return the Session-owned memory review control plane."""
        from nz_coder.state.memory_control import MemoryControlPlane

        manager = getattr(self.environment, "_mm", None)
        if manager is None:
            return None
        return MemoryControlPlane(manager.memory_dir, manager)

    def memory_manager(self):
        """Return the Session-owned manager for explicit user curation."""
        return getattr(self.environment, "_mm", None)

    def status_report(self, history: list[dict]) -> str:
        from nz_coder.workspace import status_report

        return status_report(self.environment, history)

    def trace_report(self) -> str:
        from nz_coder.trace import latest_trace, summarize_trace

        path = latest_trace(session_id=self.session_id) or latest_trace()
        return summarize_trace(path)

    def compact(self, history: list[dict], focus: str | None = None) -> list[dict]:
        return self.environment._compact_messages(history, focus=focus)

    def diff(self) -> str:
        from nz_coder.state.changes import render_latest_diff

        tracker = getattr(self.environment, "change_tracker", None)
        return tracker.render_diff() if tracker is not None else render_latest_diff()

    def processes(self) -> list[dict]:
        from nz_coder.runtime.process_service import workspace_process_service

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
        from nz_coder.runtime.process_service import workspace_process_service

        return workspace_process_service(self.environment.workdir).read(
            process_id,
            owner_session_id=self.session_id,
            cursor=cursor,
            tail_bytes=tail_bytes,
            max_bytes=max_bytes,
            wait_seconds=wait_seconds,
        ).to_dict()

    def process_kill(self, process_id: str) -> dict:
        from nz_coder.runtime.process_service import workspace_process_service

        return workspace_process_service(self.environment.workdir).kill(
            process_id,
            owner_session_id=self.session_id,
        ).to_dict()

    def undo(self, history: list[dict]):
        return self.environment.revert_message(history)

    def redo(self, history: list[dict]):
        return self.environment.unrevert_message(history)

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
        provider_override = None
        model_override = model
        if isinstance(model, str) and "/" in model:
            provider_override, model_override = model.split("/", 1)
        request = run_request_from_legacy_host(
            self.environment,
            messages,
            stream,
            allowed_tools=allowed_tools,
            provider_override=provider_override,
            model_override=model_override,
        )
        self._active_task = asyncio.current_task()
        try:
            result = await self._client.run(
                request,
                on_tool=on_tool,
                on_text=on_text,
                on_token=on_token,
            )
        finally:
            self._active_task = None
        messages[:] = copy.deepcopy(list(result.messages))
        return {
            "status": result.status.value,
            "last_error": result.error,
            "runtime": copy.deepcopy(result.metadata.get("runtime", {})),
        }

    def close(self) -> None:
        """Close the current product environment exactly once."""
        from nz_coder.runtime.process_service import dispose_session_processes
        from nz_coder.runtime.workdir import current_workdir

        dispose_session_processes(current_workdir(), self.session_id)
        close = getattr(self.environment, "close", None)
        if callable(close):
            close()


__all__ = ["TerminalSessionController"]
