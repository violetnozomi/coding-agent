"""Interactive Workflow command with generation, approval, and lifecycle control."""
from __future__ import annotations

import asyncio
import json

from rich.markup import escape
from rich.table import Table

from nz_coder.interface.commands.registry import Command, CommandContext, CommandRegistry
from nz_coder.runtime.workflow_host import (
    build_workflow_approval_summary,
    workflow_approval_digest,
)
from nz_coder.runtime.workflow_resolver import resolve_workflow_capsule
from nz_coder.runtime.workflow_run_store import (
    list_workflow_run_records,
    read_workflow_run_record,
)
from nz_coder.runtime.workflow_sdk import WorkflowHostSDK, WorkflowStartError


def register_workflow_commands(registry: CommandRegistry) -> None:
    registry.register(Command(
        "workflow",
        "Generate, run, inspect, and control multi-Agent workflows",
        "/workflow [list|show ID|run NAME [JSON_ARGS|REQUEST]|generate REQUEST|pause ID|resume ID|stop ID]",
        handle_workflow,
        aliases=("workflows",),
        category="Agent",
        suggested=True,
    ))


def _manager(ctx: CommandContext):  # noqa: ANN001
    manager = getattr(ctx.agent, "background_agents", None)
    if manager is None:
        raise RuntimeError("Session Workflow manager is unavailable")
    return manager


def _runs_root(manager):  # noqa: ANN001
    return manager._workflow.root / "runs"


def _render_runs(ctx: CommandContext) -> None:
    manager = _manager(ctx)
    active = manager.workflow_run_snapshots()
    active_ids = {str(item.get("run_id") or "") for item in active}
    persisted = [
        item for item in list_workflow_run_records(_runs_root(manager), 100)
        if str(item.get("run_id") or "") not in active_ids
    ]
    rows = [*active, *persisted]
    table = Table(title="Workflow runs", expand=True)
    table.add_column("Run")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Phases")
    for item in rows[:100]:
        table.add_row(
            str(item.get("run_id") or ""),
            str(item.get("name") or item.get("display_name") or item.get("workflow_name") or "workflow"),
            str(item.get("status") or "unknown"),
            ", ".join(str(value) for value in item.get("phase_names") or []),
        )
    ctx.console.print(table if rows else "[info]No workflow runs.[/info]")


async def _approve(ctx: CommandContext, summary: dict) -> str:
    if ctx.terminal_input is None:
        return "cancel"
    phases = ", ".join(str(item) for item in summary.get("phases") or [])
    result = await ctx.terminal_input.select_async(
        title="Workflow approval",
        text=(
            f"{summary.get('name', 'workflow')} — {summary.get('description', '')}\n"
            f"Phases: {phases or 'unspecified'}\n"
            f"Agents: {summary.get('planned_agents', 'unspecified')} planned; "
            f"concurrency {summary.get('max_concurrency', 'unspecified')}\n"
            f"Risk: {'may write files' if summary.get('writes_files') else 'read-only'}\n"
            "Approve this exact plan?"
        ),
        values=[
            ("approve", "Approve and start"),
            ("deny", "Deny this workflow"),
            ("cancel", "Cancel without starting"),
        ],
    )
    return str(result) if result in {"approve", "deny", "cancel"} else "cancel"


async def _start_plan(ctx: CommandContext, plan: dict, *, display_name: str = "") -> None:
    manager = _manager(ctx)
    manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
    summary = build_workflow_approval_summary(
        manifest,
        system_max_agents=manager.agent_cap,
        system_max_concurrency=manager.concurrency_cap,
    )
    decision = await _approve(ctx, summary)
    if decision != "approve":
        ctx.console.print(
            "[info]Workflow denied.[/info]" if decision == "deny"
            else "[info]Workflow cancelled.[/info]"
        )
        return
    handle = WorkflowHostSDK(manager).start(
        plan=plan,
        display_name=display_name,
        approval_decision="approve",
        approval_digest=workflow_approval_digest(summary),
    )
    handles = ctx.session_state.setdefault("workflow_handles", {})
    handles[handle.run_id] = handle
    handle.add_done_callback(lambda settled: handles.pop(settled.run_id, None))
    try:
        started = await asyncio.to_thread(handle.wait_started, 10.0)
    except WorkflowStartError as exc:
        ctx.console.print(f"[error]{escape(str(exc))}[/error]")
        return
    ctx.console.print(
        f"[success]Started workflow {escape(str(started.get('name') or 'workflow'))} "
        f"({escape(handle.run_id)}).[/success] Use /workflow show {escape(handle.run_id)}."
    )


async def handle_workflow(ctx: CommandContext) -> None:
    parts = ctx.args.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    argument = parts[1].strip() if len(parts) > 1 else ""
    manager = _manager(ctx)
    if action == "list":
        _render_runs(ctx)
        return
    if action == "show":
        if not argument:
            ctx.console.print("[error]Usage: /workflow show RUN_ID[/error]")
            return
        active = next(
            (item for item in manager.workflow_run_snapshots() if item["run_id"] == argument),
            None,
        )
        try:
            value = active or read_workflow_run_record(_runs_root(manager), argument)
        except ValueError as exc:
            ctx.console.print(f"[error]{escape(str(exc))}[/error]")
            return
        ctx.console.print_json(json.dumps(value, ensure_ascii=False, default=str))
        return
    if action in {"pause", "resume", "stop"}:
        if not argument:
            ctx.console.print(f"[error]Usage: /workflow {action} RUN_ID[/error]")
            return
        callback = {
            "pause": manager.pause_workflow_run,
            "resume": manager.resume_workflow_run,
            "stop": manager.stop_workflow_run,
        }[action]
        ok = callback(argument)
        ctx.console.print(
            f"[success]Workflow {action} accepted.[/success]" if ok
            else f"[error]Workflow cannot {action}: {escape(argument)}[/error]"
        )
        return
    if action == "generate":
        if not argument:
            ctx.console.print("[error]Usage: /workflow generate REQUEST[/error]")
            return
        ctx.console.print("[info]Generating bounded workflow…[/info]")
        generated = await ctx.agent.generate_workflow(argument)
        if generated["kind"] == "declined":
            ctx.console.print(
                f"[info]Workflow not created: {escape(generated['reason'])}[/info]"
            )
            return
        await _start_plan(
            ctx,
            generated["capsule"]["plan"],
            display_name=generated["capsule"]["manifest"]["name"],
        )
        return
    if action == "run":
        if not argument:
            ctx.console.print("[error]Usage: /workflow run SAVED_OR_BUILTIN_NAME[/error]")
            return
        target_parts = argument.split(maxsplit=1)
        target = target_parts[0]
        raw_args = target_parts[1].strip() if len(target_parts) > 1 else ""
        capsule_args = {}
        if raw_args:
            try:
                decoded = json.loads(raw_args)
            except json.JSONDecodeError:
                decoded = {"question": raw_args}
            if not isinstance(decoded, dict):
                ctx.console.print("[error]Workflow arguments must be a JSON object.[/error]")
                return
            capsule_args = decoded
        try:
            resolved = resolve_workflow_capsule(
                target, capsule_args, workspace=manager.workspace
            )
        except Exception as exc:
            ctx.console.print(f"[error]{escape(str(exc))}[/error]")
            return
        await _start_plan(
            ctx,
            resolved["capsule"]["plan"],
            display_name=resolved["capsule"]["manifest"]["name"],
        )
        return
    ctx.console.print(
        "[error]Usage: /workflow [list|show ID|run NAME [JSON_ARGS|REQUEST]|generate REQUEST|"
        "pause ID|resume ID|stop ID][/error]"
    )
