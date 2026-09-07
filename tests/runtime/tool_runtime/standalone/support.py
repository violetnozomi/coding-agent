"""Small declared dependencies for the real tool pipeline, without a product host."""

from __future__ import annotations

from dataclasses import replace

from nz_coder.runtime.core.tool_context import (
    ToolExecutionContext,
    ToolLifecycleContext,
    ToolPolicyContext,
    ToolProjectionContext,
)
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.observers import NoOpToolObserver
from nz_coder.runtime.tool_runtime.operations import parse_tool_input
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    is_transactional_write_tool,
)


class Permissions:
    def __init__(self, behavior="allow"):
        self.behavior = behavior

    def ask_special(self, kind, metadata):
        return False

    def check(self, name, arguments):
        return {"behavior": self.behavior, "reason": "contract denial"}


class Transaction:
    def __init__(self):
        self.active = False
        self.finishes = []

    def begin(self) -> None:
        self.active = True

    def finish(
        self, has_write: bool, all_succeeded: bool, messages: list[dict]
    ) -> None:
        self.finishes.append((has_write, all_succeeded))
        self.active = False


class Executor:
    def __init__(self):
        self.calls = []

    def execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        self.calls.append(tool_call["id"])
        name = tool_call["function"]["name"]
        arguments = parse_tool_input(tool_call["function"]["arguments"])
        return ToolExecutionResult(
            name,
            arguments,
            "visible output",
            True,
            False,
            False,
            is_transactional_write_tool(name),
        )


class Dependencies:
    """Test-owned capabilities, not host private methods or a business coordinator."""

    def __init__(self):
        self.transactions = Transaction()
        self.executor = Executor()
        self.statuses = []
        self.traces = []
        self.recorded = []
        self.messages = [
            {"role": "user", "content": "Do the task", "_nz_message_id": "msg-user"},
            {"role": "assistant", "content": "", "_nz_message_id": "msg-step"},
        ]
        self.processor = SessionProcessor(self.messages[-1])
        self.processor.start_step()

    def prepare(self, calls: list[dict]) -> None:
        """Match Runner's model-admission boundary before ToolRuntime dispatch."""
        self.processor.register_tool_calls(calls)

    def trace(self, event: str, **payload: object) -> None:
        self.traces.append((event, payload))

    async def checkpoint(self, messages: list[dict], status: str) -> None:
        assert messages is self.messages
        self.statuses.append(status)

    async def before(
        self, call: dict, messages: list[dict]
    ) -> tuple[dict, ToolExecutionResult | None]:
        return call, None

    async def drain_progress(self) -> None:
        """This explicit fixture has no asynchronous progress producer."""

    async def after(
        self, call: dict, result: ToolExecutionResult, messages: list[dict]
    ) -> ToolExecutionResult:
        return result

    async def describe(self, dispatched: list, messages: list[dict]) -> bool:
        return False

    async def transition(self, signal, messages, processor) -> dict | None:
        return None

    def record(self, result: ToolExecutionResult) -> bool:
        self.recorded.append(result)
        return result.dispatch_failed

    def context(self, **lifecycle_changes) -> ToolExecutionContext:
        lifecycle = ToolLifecycleContext(
            checkpoint=self.checkpoint,
            drain_progress=self.drain_progress,
            processor_for_messages=lambda messages: self.processor,
            transaction=self.transactions,
            metadata_reporter=lambda processor, messages: lambda title, data: None,
            question_reporter=lambda processor, messages: lambda action, data: None,
            model_capabilities=None,
            describe_read_results=self.describe,
            strict_completed=lambda dispatched: False,
            apply_transition=self.transition,
            observer=NoOpToolObserver(),
            has_pre_tool_hooks=lambda: False,
            executor=self.executor,
            execute_one=lambda call, index, messages: self.executor.execute_one(
                call, index
            ),
            before_tool=self.before,
            after_tool=self.after,
            trace=self.trace,
        )
        return ToolExecutionContext(
            run=None,
            policy=ToolPolicyContext(
                agent_name="coder",
                agent_graph=None,
                tool_allowlist=None,
                admission_handle=None,
                runtime_state=None,
                recovery=RecoveryState(),
                permissions=Permissions(),
                stall_orchestrator=None,
                parse_input=parse_tool_input,
                trace=self.trace,
            ),
            lifecycle=replace(lifecycle, **lifecycle_changes),
            projection=ToolProjectionContext(
                signal_from_metadata=lambda metadata: None,
                record_result=self.record,
                trace_result=lambda result, output, tool_call_id="", index=None: None,
                stall_orchestrator=None,
                after_result=lambda messages, result, output: None,
            ),
        )


def call(name="write_file", identity="call-write", **arguments):
    return {
        "id": identity,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments or {"path": "a.txt", "content": "written"},
        },
    }
