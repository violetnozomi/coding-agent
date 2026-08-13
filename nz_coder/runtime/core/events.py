"""Host-neutral event contracts emitted by the shared Agent runtime."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeEventName(str, Enum):
    """Stable lifecycle names shared by terminal, HTTP, SDK, and traces."""

    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    RUN_STARTED = "session.run.started"
    RUN_COMPLETED = "session.run.completed"
    RUN_FAILED = "session.run.failed"
    RUN_CANCELLED = "session.run.cancelled"
    MODEL_STARTED = "session.model.started"
    MODEL_FINISHED = "session.model.finished"
    MODEL_FAILED = "session.model.failed"
    TOOL_STARTED = "session.tool.started"
    TOOL_FINISHED = "session.tool.finished"
    TOOL_FAILED = "session.tool.failed"
    CHILD_STARTED = "session.child.started"
    CHILD_FINISHED = "session.child.finished"


@dataclass(frozen=True)
class RuntimeEvent:
    """Immutable, correlation-safe lifecycle fact for hosts and trace sinks."""

    name: RuntimeEventName | str
    run_id: str
    session_id: str
    payload: dict = field(default_factory=dict)
    agent_id: str = ""
    parent_run_id: str = ""
    timestamp: str = field(default_factory=_utc_timestamp)

    def __post_init__(self) -> None:
        if isinstance(self.name, RuntimeEventName):
            object.__setattr__(self, "name", self.name)
        elif not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("RuntimeEvent name must be non-empty")
        for field_name in ("run_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"RuntimeEvent {field_name} must be non-empty")
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))


@runtime_checkable
class RuntimeEventSink(Protocol):
    """Destination for ordered runtime lifecycle facts."""

    def publish(self, event: RuntimeEvent) -> None:
        """Record or publish one event without changing Runner state."""
        ...


class RuntimeEventMiddleware:
    """Emit canonical events while keeping sink failures out of Agent control flow."""

    def __init__(self, sink: RuntimeEventSink) -> None:
        self.sink = sink

    async def before_run(self, context) -> None:
        opened = getattr(context, "metadata", {}).get("session_open")
        if opened in {"created", "resumed"}:
            self._emit(
                context,
                RuntimeEventName.SESSION_CREATED
                if opened == "created" else RuntimeEventName.SESSION_RESUMED,
            )
        self._emit(context, RuntimeEventName.RUN_STARTED)

    async def after_run(self, context, result) -> None:
        status = result.get("status") if isinstance(result, dict) else "completed"
        name = (
            RuntimeEventName.RUN_CANCELLED
            if status in {"cancelled", "interrupted"}
            else RuntimeEventName.RUN_FAILED
            if status in {"error", "blocked"}
            else RuntimeEventName.RUN_COMPLETED
        )
        self._emit(context, name, {"status": str(status)})

    async def on_run_error(self, context, error) -> None:
        self._emit(context, RuntimeEventName.RUN_FAILED, {"error": str(error)})

    async def before_model(self, context) -> None:
        self._emit(context, RuntimeEventName.MODEL_STARTED)

    async def after_model(self, context, _result) -> None:
        self._emit(context, RuntimeEventName.MODEL_FINISHED)

    async def on_model_error(self, context, error) -> None:
        self._emit(context, RuntimeEventName.MODEL_FAILED, {"error": str(error)})

    async def before_tool_batch(self, context) -> None:
        self._emit(context, RuntimeEventName.TOOL_STARTED)

    async def after_tool_batch(self, context, _result) -> None:
        self._emit(context, RuntimeEventName.TOOL_FINISHED)

    async def on_tool_batch_error(self, context, error) -> None:
        self._emit(context, RuntimeEventName.TOOL_FAILED, {"error": str(error)})

    def _emit(self, context, name: RuntimeEventName, payload: dict | None = None) -> None:
        request = context.request
        metadata = getattr(context, "metadata", {})
        if metadata.get("suppress_runtime_events"):
            return
        run_id = str(metadata.get("run_id") or request.session_id)
        try:
            self.sink.publish(RuntimeEvent(
                name=name,
                run_id=run_id,
                session_id=request.session_id,
                agent_id=getattr(request.agent, "name", ""),
                parent_run_id=str(request.parent_run_id or ""),
                payload=payload or {},
            ))
        except Exception:
            return


__all__ = [
    "RuntimeEvent", "RuntimeEventMiddleware", "RuntimeEventName", "RuntimeEventSink",
]
