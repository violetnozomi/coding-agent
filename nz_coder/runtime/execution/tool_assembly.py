"""Product composition for focused tool capabilities, rebuilt after run owner resets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from pathlib import Path

from nz_coder.runtime.core.tool_contracts import (
    DispatchedTools,
    TextDisplay,
    ToolBatchState,
)
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.agent.handoffs import HandoffSignal
from nz_coder.intelligence.code_index import IndexStats

if TYPE_CHECKING:
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

from nz_coder.runtime.core.tool_context import (
    ToolExecutionContext,
    ToolLifecycleContext,
    ToolPolicyContext,
    ToolProjectionContext,
)
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.execution.tool_effects import (
    CodingWriteEffects,
    ToolResultRecorder,
    ToolTransaction,
)
from nz_coder.runtime.process.tool_snapshots import ToolStepSnapshots
from nz_coder.runtime.session.tool_progress import ToolSessionBoundary
from nz_coder.runtime.session.session_repository import FileSessionRepository
from nz_coder.runtime.tool_runtime.observers import CodingToolObserver
from nz_coder.runtime.tool_runtime.operations import (
    HookedToolExecutor,
    infer_tool_path,
    parse_tool_input,
)
from nz_coder.intelligence.code_index import update_code_index_after_write
from nz_coder.lsp.write_diagnostics import collect_write_diagnostics


def result_recorder(environment: ProductRunEnvironment) -> ToolResultRecorder:
    """Bind current run-owned objects, never copied lifecycle counters."""
    return ToolResultRecorder(
        facts=environment.tool_run_facts,
        vm=environment.vm,
        runtime_state=environment.runtime_state,
        scratchpad=environment._sp,
        skills=environment._skill_loader,
        run_evidence=environment.run_evidence,
        admission=environment._admission_session,
        lineage=environment.lineage,
        run_id=environment.tracer.run_id,
        trace=environment.tracer.log,
        publish=_publisher(environment),
    )


def write_effects(
    environment: ProductRunEnvironment,
    *,
    refresh_index: Callable[
        [list[str], Path], IndexStats
    ] = update_code_index_after_write,
    diagnostics: Callable[[list[str], Path], str] = collect_write_diagnostics,
) -> CodingWriteEffects:
    return CodingWriteEffects(
        workspace=environment.workdir,
        change_tracker=environment.change_tracker,
        runtime_state=environment.runtime_state,
        run_evidence=environment.run_evidence,
        recovery=environment.recovery,
        admission=environment._admission_session,
        project_profile=environment._project_profile_data,
        refresh_index=refresh_index,
        diagnostics=diagnostics,
        trace=environment.tracer.log,
    )


def _publisher(environment: ProductRunEnvironment) -> Callable[[str, dict], None]:
    publisher = environment.event_publisher

    def publish(event: str, payload: dict) -> None:
        try:
            publisher.publish(event, payload)
        except Exception:
            pass

    return publish


def build_tool_execution_context(
    environment: ProductRunEnvironment,
    run: RunContext | None,
    services: RuntimeServices,
    *,
    sync: bool = False,
    refresh_index: Callable[
        [list[str], Path], IndexStats
    ] = update_code_index_after_write,
    diagnostics: Callable[[list[str], Path], str] = collect_write_diagnostics,
) -> ToolExecutionContext:
    """The composition root may inspect owners; tool coordination may not."""
    if (
        services.session_runtime is None
        or environment.executor is None
        or environment.txn is None
    ):
        raise TypeError(
            "Tool composition requires SessionRuntime, executor and transaction"
        )
    if run is None and getattr(environment, "active_run_context", None) is not None:
        raise RuntimeError(
            "Active Tool Runtime requires a SessionRuntime checkpoint callback"
        )
    trace = environment.tracer.log
    policy = ToolPolicyContext(
        agent_name=environment.current_agent_name or "worker",
        agent_graph=environment.agent_graph,
        tool_allowlist=(
            frozenset(environment.tool_allowlist)
            if environment.tool_allowlist is not None
            else None
        ),
        admission_handle=environment.admission_handle,
        runtime_state=environment.runtime_state,
        recovery=environment.recovery,
        permissions=environment.permissions,
        stall_orchestrator=environment.stall_orchestrator,
        parse_input=parse_tool_input,
        trace=trace,
        observability=environment._tool_observability,
    )
    bridges = ProductToolBridges(environment, services, policy, sync=sync)
    recorder = result_recorder(environment)
    effects = write_effects(
        environment, refresh_index=refresh_index, diagnostics=diagnostics
    )
    snapshots = ToolStepSnapshots(environment.workspace_snapshots, trace)
    repository = FileSessionRepository()

    def progress_checkpoint(messages: list[dict], status: str) -> None:
        repository.checkpoint(environment, messages, status)

    async def checkpoint(messages: list[dict], status: str) -> None:
        if run is None:
            progress_checkpoint(messages, status)
        else:
            if messages is not run.transcript:
                raise ValueError(
                    "Tool checkpoint transcript does not belong to this RunContext"
                )
            await services.session_runtime.checkpoint(run, status)

    session = ToolSessionBoundary(
        checkpoint,
        progress_checkpoint if run is None else None,
        _publisher(environment),
    )
    hooked_executor = HookedToolExecutor(environment.executor, bridges.before_hook)
    observer = CodingToolObserver(
        post_write=effects.post_write,
        after_batch=bridges.after_batch,
        snapshots=snapshots,
        plan_mode=environment.plan_mode,
        terminal_summary=environment.tool_run_facts.set_terminal_summary,
        trace=trace,
    )
    return ToolExecutionContext(
        run=run,
        policy=policy,
        lifecycle=ToolLifecycleContext(
            checkpoint=session.checkpoint,
            drain_progress=session.drain_progress,
            processor_for_messages=session.processor_for_messages,
            transaction=ToolTransaction(environment.txn, trace),
            metadata_reporter=session.metadata_reporter,
            question_reporter=session.question_reporter,
            model_capabilities=environment.model_capabilities,
            describe_read_results=bridges.describe_read_results,
            strict_completed=recorder.strict_completed,
            apply_transition=bridges.apply_transition,
            observer=observer,
            has_pre_tool_hooks=environment.hooks.has_pre_tool_use_hooks,
            executor=environment.executor,
            execute_one=hooked_executor.execute_one,
            before_tool=bridges.before_tool,
            after_tool=bridges.after_tool,
            trace=trace,
        ),
        projection=ToolProjectionContext(
            signal_from_metadata=bridges.signal_from_metadata,
            record_result=recorder.record_result,
            trace_result=recorder.trace_result,
            stall_orchestrator=environment.stall_orchestrator,
            after_result=bridges.after_result,
            available_result_tokens=bridges.available_result_tokens,
            runtime_state=environment.runtime_state,
        ),
    )


class ProductToolBridges:
    """Frozen migration exceptions: host-based Hooks, guardrails, input, transition and prompt budget.

    No transaction, result-recording or committed-write rule lives here. These
    existing outer APIs still require the product owner; the tool core never sees it.
    """

    def __init__(
        self,
        environment: ProductRunEnvironment,
        services: RuntimeServices,
        policy: ToolPolicyContext,
        *,
        sync: bool = False,
    ) -> None:
        self.environment = environment
        self.services = services
        self.policy = policy
        self.sync = sync

    def before_hook(self, messages, name, arguments, path, is_write):
        return self.environment.hooks.before_tool_use(
            self.environment,
            messages,
            name,
            arguments,
            file_path=path,
            is_write=is_write,
        )

    async def before_tool(
        self, call: dict, messages: list[dict]
    ) -> tuple[dict, ToolExecutionResult | None]:
        guardrails = self.services.guardrails
        if self.sync:
            return await guardrails.before_tool_sync(self.environment, call, messages)
        return await guardrails.before_tool(self.environment, call, messages)

    async def after_tool(
        self, call: dict, result: ToolExecutionResult, messages: list[dict]
    ) -> ToolExecutionResult:
        return await self.services.guardrails.after_tool(
            self.environment, call, result, messages
        )

    async def describe_read_results(
        self, dispatched: DispatchedTools, messages: list[dict]
    ) -> bool:
        return await self.services.inputs.describe_read_results(
            self.environment, dispatched, messages
        )

    def signal_from_metadata(self, metadata: dict | None) -> HandoffSignal | None:
        return self.services.transitions.signal_from_metadata(
            self.environment, metadata
        )

    async def apply_transition(
        self,
        signal: HandoffSignal,
        messages: list[dict],
        processor: SessionProcessor | None,
    ) -> dict | None:
        transition = self.services.transitions.apply(
            self.environment, signal, messages, processor
        )
        self.policy.agent_name = (
            self.environment.current_agent_name or self.policy.agent_name
        )
        if self.sync:
            self.environment._notify_agent_switched(transition)
        else:
            await self.environment._notify_agent_switched_async(transition)
        return transition

    def after_result(
        self, messages: list[dict], result: ToolExecutionResult, output: str
    ) -> None:
        self.environment.hooks.after_tool_result(
            self.environment, messages, result, output
        )
        self.environment.hooks.on_post_tool_use(
            self.environment,
            messages,
            result.name,
            result.tool_input,
            file_path=infer_tool_path(result.tool_input),
            output=output,
            status="error" if result.dispatch_failed else "ok",
            is_write=result.is_write,
        )

    def after_batch(
        self, messages: list[dict], state: ToolBatchState, on_text: TextDisplay | None
    ) -> None:
        self.environment.hooks.after_tool_batch(
            self.environment,
            messages,
            manual_compact=state["manual_compact"],
            used_todo=state["used_todo"],
            on_text=on_text,
            write_total=state["write_total"],
            write_denied=state["write_denied"],
        )

    def available_result_tokens(self, messages: list[dict]) -> int:
        budget = self.environment._prompt_budget()
        if budget.usable_input_tokens <= 0:
            return 16_000
        return max(
            1,
            budget.usable_input_tokens
            - self.environment._projected_request_tokens(messages),
        )
