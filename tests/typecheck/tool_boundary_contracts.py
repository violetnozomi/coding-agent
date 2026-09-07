"""Type-check actual tool-side call shapes; not a runtime test substitute."""

from __future__ import annotations

from nz_coder.runtime.core.tool_context import ToolLifecycleContext
from nz_coder.runtime.core.contracts import ToolRuntime
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.runtime.core.tool_contracts import (
    ToolExecutorPort,
    ToolTransactionPort,
    ToolBatchObserver,
    ToolRecoveryPort,
    ToolStallPort,
)
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.execution.tool_effects import ToolTransaction
from nz_coder.runtime.tool_runtime.observers import CodingToolObserver
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator


async def checkpoint_call(
    lifecycle: ToolLifecycleContext, messages: list[dict]
) -> None:
    await lifecycle.checkpoint(messages, "running")


def real_ports(
    executor: ToolExecutor, transaction: ToolTransaction, observer: CodingToolObserver
) -> tuple[ToolRuntime, ToolExecutorPort, ToolTransactionPort, ToolBatchObserver]:
    tool_runtime: ToolRuntime = ProductionToolRuntime()
    tool_executor: ToolExecutorPort = executor
    tool_transaction: ToolTransactionPort = transaction
    tool_observer: ToolBatchObserver = observer
    return tool_runtime, tool_executor, tool_transaction, tool_observer


def observation_ports(
    recovery: RecoveryState, stall: StallSidecarOrchestrator
) -> tuple[ToolRecoveryPort, ToolStallPort]:
    return recovery, stall
