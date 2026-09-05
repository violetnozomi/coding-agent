"""Provider-free contract suite for core coding-agent production APIs.

This module verifies integration contracts.  It is intentionally distinct from
``AgentBehaviorBenchmark``, where an AgentRunner must solve the task itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from nz_coder.intelligence.code_index import PersistentCodeIndex
from nz_coder.intelligence.repository_graph import RepositoryGraph
from nz_coder.evaluation.native_scenario import run_native_long_horizon
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.tool_platform.catalog import ToolCatalog
from nz_coder.tool_platform.exposure import ContextPressure, ToolExposurePlanner
from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector
from nz_coder.tool_platform.search import ToolSearchIndex


@dataclass(frozen=True)
class BenchmarkCase:
    """One stable benchmark scenario and its production entry point."""

    case_id: str
    capability: str
    production_api: str


def benchmark_manifest() -> tuple[BenchmarkCase, ...]:
    """Return the versioned A-H core-capability manifest."""
    return (
        BenchmarkCase("A", "unknown-localization", "PersistentCodeIndex.symbol_context"),
        BenchmarkCase("B", "cross-file-impact", "PersistentCodeIndex.callers"),
        BenchmarkCase("C", "large-repo-navigation", "RepositoryGraph.overview"),
        BenchmarkCase("D", "large-tool-catalog", "ToolExposurePlanner.plan"),
        BenchmarkCase("E", "huge-tool-output", "ToolResultProjector.project_batch"),
        BenchmarkCase("F", "long-horizon", "AgentTrajectoryMetrics.from_events"),
        BenchmarkCase("G", "verification-recovery", "AgentTrajectoryMetrics.from_events"),
        BenchmarkCase("H", "multi-agent", "AgentTrajectoryMetrics.from_events"),
    )


@dataclass(frozen=True)
class AgentTrajectoryMetrics:
    """Unified aggregate computed from production JSONL trace events."""

    success: bool | None = None
    patch_valid: bool | None = None
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    schema_tokens: int = 0
    tool_result_tokens: int = 0
    repo_intelligence_calls: int = 0
    searches: int = 0
    reads: int = 0
    duplicate_reads: int = 0
    duplicate_searches: int = 0
    failed_commands: int = 0
    backtracks: int = 0
    compactions: int = 0
    verification_attempts: int = 0
    verification_recoveries: int = 0
    child_sessions: int = 0
    conflicts: int = 0
    wall_time_ms: float = 0.0
    cost: float = 0.0
    semantic_search_calls: int = 0
    web_search_calls: int = 0
    webfetch_calls: int = 0
    structural_lookup_calls: int = 0
    retrieval_fallbacks: int = 0
    first_retrieval_tool: str = ""
    localization_turn: int | None = None
    time_to_first_correct_file_ms: float | None = None
    grep_before_ri: bool = False
    ri_before_grep: bool = False
    retrieval_precision: float = 0.0
    ri_candidate_precision: float = 0.0
    process_start_count: int = 0
    process_read_count: int = 0
    process_write_count: int = 0
    process_status_count: int = 0
    process_resize_count: int = 0
    process_kill_count: int = 0
    wrong_process_access: int = 0
    orphan_process_count: int = 0
    buffer_bytes: int = 0
    process_projection_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_jsonl(cls, path: Path) -> "AgentTrajectoryMetrics":
        events = []
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return cls.from_events(events)

    @classmethod
    def from_events(cls, events: list[dict]) -> "AgentTrajectoryMetrics":
        values = {field.name: field.default for field in cls.__dataclass_fields__.values()}
        seen_reads: set[str] = set()
        seen_searches: set[str] = set()
        verification_failed = False
        first_retrieval_index: int | None = None
        first_grep_index: int | None = None
        first_ri_index: int | None = None
        for event_index, event in enumerate(events):
            kind = str(event.get("event") or event.get("type") or "")
            if kind in {"model_call", "llm_response"}:
                values["model_calls"] += 1
                values["turns"] += 1
                values["input_tokens"] += int(event.get("input_tokens") or 0)
                values["output_tokens"] += int(event.get("output_tokens") or 0)
                values["schema_tokens"] += int(event.get("schema_tokens") or 0)
                values["cost"] += float(event.get("cost") or 0.0)
            elif kind in {"tool_result", "tool_call"}:
                name = str(event.get("tool_name") or event.get("name") or "")
                values["tool_calls"] += 1
                values["tool_result_tokens"] += int(
                    event.get("tokens") or (int(event.get("output_len") or 0) + 3) // 4
                )
                if name in {
                    "repo_context", "repo_map", "symbol_context", "process_context",
                    "code_references", "smart_search", "semantic_search",
                }:
                    values["repo_intelligence_calls"] += 1
                if name == "semantic_search":
                    values["semantic_search_calls"] += 1
                if name == "web_search":
                    values["web_search_calls"] += 1
                if name == "webfetch":
                    values["webfetch_calls"] += 1
                raw_input = event.get("input")
                if name == "process":
                    operation = (
                        str(raw_input.get("operation") or "").strip().lower()
                        if isinstance(raw_input, dict) else ""
                    )
                    if operation == "list":
                        operation = "status"
                    metric = {
                        "start": "process_start_count",
                        "read": "process_read_count",
                        "write": "process_write_count",
                        "status": "process_status_count",
                        "resize": "process_resize_count",
                        "kill": "process_kill_count",
                    }.get(operation)
                    if metric:
                        values[metric] += 1
                    output = str(event.get("output") or "")
                    lowered = output.casefold()
                    values["wrong_process_access"] += int(
                        "unknown process_id" in lowered
                        or "belongs to another session" in lowered
                    )
                    if operation == "read":
                        values["process_projection_count"] += int(
                            "\"has_more\": true" in lowered
                            or "\"truncated_before_cursor\": true" in lowered
                        )
                        try:
                            payload = json.loads(output)
                        except (TypeError, ValueError):
                            payload = {}
                        if isinstance(payload, dict):
                            values["buffer_bytes"] = max(
                                values["buffer_bytes"],
                                int(payload.get("buffer_end_cursor") or 0)
                                - int(payload.get("buffer_start_cursor") or 0),
                            )
                if (
                    name == "repo_context" and isinstance(raw_input, dict)
                    and raw_input.get("operation") == "lookup"
                ):
                    values["structural_lookup_calls"] += 1
                if name in {
                    "grep", "grep_search", "smart_search", "repo_context", "repo_map",
                    "code_references", "semantic_search", "read", "read_file",
                } and first_retrieval_index is None:
                    first_retrieval_index = event_index
                    values["first_retrieval_tool"] = name
                if name in {"grep", "grep_search", "smart_search"} and first_grep_index is None:
                    first_grep_index = event_index
                if name in {
                    "repo_context", "repo_map", "code_references", "semantic_search",
                } and first_ri_index is None:
                    first_ri_index = event_index
                output = str(event.get("output") or "")
                if (
                    name in {"repo_context", "semantic_search"}
                    and ("\"fallback\": true" in output.casefold()
                         or "\"freshness\": \"cold\"" in output.casefold()
                         or "\"freshness\": \"stale\"" in output.casefold())
                ):
                    values["retrieval_fallbacks"] += 1
                if name in {"read", "read_file"}:
                    values["reads"] += 1
                    raw_input = event.get("input")
                    input_path = raw_input.get("path") if isinstance(raw_input, dict) else raw_input
                    key = str(event.get("path") or input_path or "")
                    values["duplicate_reads"] += int(key in seen_reads)
                    seen_reads.add(key)
                if name in {"grep", "grep_search", "smart_search"}:
                    values["searches"] += 1
                    raw_input = event.get("input")
                    input_query = raw_input.get("query") if isinstance(raw_input, dict) else raw_input
                    key = str(event.get("query") or input_query or "")
                    values["duplicate_searches"] += int(key in seen_searches)
                    seen_searches.add(key)
                values["failed_commands"] += int(
                    name == "bash" and bool(
                        event.get("failed")
                        or event.get("command_failed")
                        or event.get("status") in {"error", "nonzero"}
                    )
                )
            elif kind in {"verification", "verification_result"}:
                values["verification_attempts"] += 1
                succeeded = bool(event.get("success") or event.get("passed"))
                if succeeded and verification_failed:
                    values["verification_recoveries"] += 1
                    verification_failed = False
                elif not succeeded:
                    verification_failed = True
            elif kind == "llm_request":
                values["schema_tokens"] += int(event.get("schema_tokens") or 0)
            elif kind == "compaction" or kind.startswith("context_compaction"):
                values["compactions"] += 1
            elif kind in {"child_session", "subagent", "subagent_spawn"}:
                values["child_sessions"] += 1
                values["conflicts"] += int(event.get("conflicts") or 0)
            elif kind in {"backtrack", "replan"}:
                values["backtracks"] += 1
            elif kind in {"run_complete", "result", "run_end"}:
                if "success" in event:
                    values["success"] = bool(event["success"])
                elif "status" in event:
                    values["success"] = str(event["status"]) in {
                        "completed", "completed_unverified",
                    }
                if "patch_valid" in event:
                    values["patch_valid"] = bool(event["patch_valid"])
                values["wall_time_ms"] = float(event.get("wall_time_ms") or values["wall_time_ms"])
            elif kind == "process_benchmark":
                values["orphan_process_count"] = int(
                    event.get("orphan_process_count") or 0
                )
        values["grep_before_ri"] = bool(
            first_grep_index is not None
            and (first_ri_index is None or first_grep_index < first_ri_index)
        )
        values["ri_before_grep"] = bool(
            first_ri_index is not None
            and (first_grep_index is None or first_ri_index < first_grep_index)
        )
        return cls(**values)


@dataclass(frozen=True)
class TrajectoryDiagnostics:
    """Actionable anti-pattern counts derived from an Agent event stream."""

    tool_selection_errors: int = 0
    premature_compactions: int = 0
    verification_loops: int = 0
    backtracks: int = 0
    repeated_reads: int = 0
    repeated_searches: int = 0
    repeated_failing_commands: int = 0
    late_localization: int = 0
    excessive_compactions: int = 0
    recommendations: tuple[str, ...] = ()


def diagnose_trajectory(events: list[dict]) -> TrajectoryDiagnostics:
    """Detect repeated misses, early compaction, backtracking, and verify loops."""
    selection_errors = premature = loops = backtracks = 0
    repeated_reads = repeated_searches = repeated_failures = late = excessive = 0
    last_input = last_window = 0
    failed_verification: dict[str, int] = {}
    looped_commands: set[str] = set()
    seen_reads: set[str] = set()
    seen_searches: set[str] = set()
    exploratory_reads = 0
    localized = False
    for event in events:
        kind = str(event.get("event") or event.get("type") or "")
        if kind in {"model_call", "llm_response"}:
            last_input = int(event.get("input_tokens") or 0)
            last_window = int(event.get("context_window") or 0)
        elif kind == "compaction" or kind.startswith("context_compaction"):
            if last_window and last_input / last_window < 0.60:
                premature += 1
            if premature + excessive >= 3:
                excessive += 1
        elif kind in {"tool_result", "tool_call"}:
            tool_name = str(event.get("tool_name") or event.get("name") or "")
            output = str(event.get("output") or "").casefold()
            failed = bool(
                event.get("failed") or event.get("dispatch_failed")
                or event.get("command_failed")
                or event.get("status") in {"error", "nonzero"}
            )
            if failed and (
                "no matches" in output or "not found" in output
                or bool(event.get("dispatch_failed"))
            ):
                selection_errors += 1
            raw_input = event.get("input")
            if tool_name in {"read", "read_file"}:
                input_path = raw_input.get("path") if isinstance(raw_input, dict) else raw_input
                path = str(event.get("path") or input_path or "")
                repeated_reads += int(path in seen_reads)
                seen_reads.add(path)
                exploratory_reads += int(not localized)
            if tool_name in {"grep", "grep_search", "smart_search"}:
                input_query = raw_input.get("query") if isinstance(raw_input, dict) else raw_input
                query = str(event.get("query") or input_query or "")
                repeated_searches += int(query in seen_searches)
                seen_searches.add(query)
            if bool(event.get("localized")):
                localized = True
                if exploratory_reads >= 5:
                    late = 1
            if tool_name == "bash" and failed:
                input_command = raw_input.get("command") if isinstance(raw_input, dict) else raw_input
                command = str(event.get("command") or input_command or "")
                failure_key = "tool:" + command
                failed_verification[failure_key] = failed_verification.get(failure_key, 0) + 1
                if failed_verification[failure_key] == 3:
                    repeated_failures += 1
        elif kind in {"verification", "verification_result"}:
            command = str(event.get("command") or "<unknown>")
            if bool(event.get("success") or event.get("passed")):
                failed_verification[command] = 0
            else:
                failed_verification[command] = failed_verification.get(command, 0) + 1
                if failed_verification[command] >= 3 and command not in looped_commands:
                    loops += 1
                    looped_commands.add(command)
        elif kind in {"backtrack", "replan"}:
            backtracks += 1
    recommendations = []
    if selection_errors:
        recommendations.append("Change search strategy after repeated empty or failed tool results.")
    if premature:
        recommendations.append("Delay compaction until measured context pressure crosses the soft limit.")
    if loops:
        recommendations.append("Do not rerun an unchanged failing verification command three times.")
    if backtracks:
        recommendations.append("Record the invalidated assumption before replanning.")
    if repeated_reads or repeated_searches:
        recommendations.append("Reuse prior read/search evidence instead of repeating identical retrieval.")
    if late:
        recommendations.append("Localize the target before broad file reading.")
    return TrajectoryDiagnostics(
        selection_errors, premature, loops, backtracks,
        repeated_reads, repeated_searches, repeated_failures, late, excessive,
        tuple(recommendations),
    )


class CoreCapabilityContractSuite:
    """Named facade for the legacy A-H production integration contracts."""

    manifest = staticmethod(benchmark_manifest)

    @staticmethod
    def run(output_dir: Path) -> dict:
        return run_local_benchmark(output_dir)


def _tool_spec(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": "benchmark tool " * 20,
        "parameters": {"type": "object", "properties": {}},
    }}


def _benchmark_tool_discovery() -> dict:
    """Measure natural-language recall, unlock, and two-turn schema cost."""
    import nz_coder.runtime.execution.loop  # noqa: F401  # register the production tool surface
    from nz_coder.tools import get_catalog_specs

    specs = get_catalog_specs()
    catalog = ToolCatalog.from_specs(specs)
    planner = ToolExposurePlanner()
    pressure = ContextPressure(128_000, 3_300, 8_000)
    initial = planner.plan(catalog, pressure=pressure)
    search = ToolSearchIndex(catalog)
    scenarios = (
        ("repo_overview", "understand repository structure modules dependencies overview",
         ("repo_context", "repo_map")),
        ("symbol_source", "read Python symbol source definition", ("read_symbol",)),
        ("symbol_callers", "find Python symbol callers references",
         ("find_symbol_callers", "code_references")),
        ("changed_verify", "verify changed source files syntax typecheck",
         ("verify_changed_files",)),
        ("workflow_execute", "run parallel multi-step workflow pipeline", ("workflow_run",)),
        ("workflow_history", "inspect saved workflow run history artifacts", ("workflow_runs",)),
        ("semantic", "locate code using business language meaning embedding similarity",
         ("semantic_search",)),
        ("project_profile", "detect repository languages package managers test commands",
         ("project_profile",)),
    )
    rows = []
    for case_id, query, targets in scenarios:
        matches = search.search(query, limit=5)
        names = tuple(item.name for item in matches)
        target_ranks = [names.index(target) + 1 for target in targets if target in names]
        ranked_target = min(
            ((names.index(target), target) for target in targets if target in names),
            default=None,
        )
        exact_matches = (
            search.search(f"select:{ranked_target[1]}", limit=1)
            if ranked_target is not None else ()
        )
        exact_names = tuple(item.name for item in exact_matches)
        next_plan = planner.plan(catalog, unlocked=exact_names, pressure=pressure)
        next_deferred = set(next_plan.deferred_names)
        result_text = "\n".join(
            json.dumps(item.definition.spec()["function"], ensure_ascii=False, sort_keys=True)
            for item in exact_matches
        )
        search_result_tokens = max(1, (len(result_text) + 3) // 4)
        baseline = catalog.schema_tokens * 2
        progressive = (
            initial.estimated_tokens_after
            + next_plan.estimated_tokens_after
            + search_result_tokens
        )
        saved = baseline - progressive
        rows.append({
            "case_id": case_id,
            "target_recalled": bool(target_ranks),
            "target_rank": min(target_ranks) if target_ranks else None,
            "next_turn_unlocked": bool(
                ranked_target is not None and ranked_target[1] not in next_deferred
            ),
            "matches": list(names),
            "search_result_tokens": search_result_tokens,
            "two_turn_token_savings": saved,
            "two_turn_token_savings_pct": round(saved / max(1, baseline) * 100, 1),
        })
    ranks = [int(row["target_rank"]) for row in rows if row["target_rank"] is not None]
    return {
        "catalog_tools": len(specs),
        "full_schema_tokens": catalog.schema_tokens,
        "initial_visible_tools": len(initial.visible_names),
        "initial_schema_tokens": initial.estimated_tokens_after,
        "discovery_recall_cases": len(rows),
        "discovery_recall_hits": sum(bool(row["target_recalled"]) for row in rows),
        "worst_target_rank": max(ranks, default=0),
        "next_turn_unlocks": sum(bool(row["next_turn_unlocked"]) for row in rows),
        "two_turn_token_savings_min_pct": min(
            (float(row["two_turn_token_savings_pct"]) for row in rows), default=0.0,
        ),
        "discovery_cases": rows,
    }


def run_local_benchmark(output_dir: Path) -> dict:
    """Exercise A-H production contracts and persist deterministic evidence."""
    started = time.perf_counter()
    root = Path(output_dir).resolve()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    stale_verification = workspace / "verification_target.py"
    if stale_verification.is_file():
        stale_verification.unlink()
    (workspace / "helpers.py").write_text("def leaf(): return 1\n", encoding="utf-8")
    (workspace / "service.py").write_text(
        "from helpers import leaf\ndef entry(): return leaf()\n", encoding="utf-8",
    )
    (workspace / "pipeline.py").write_text(
        "def normalize_input(value): return value.strip()\n", encoding="utf-8",
    )
    index = PersistentCodeIndex(workspace)
    index.scan(workspace, max_files=100)
    large_workspace = root / "large-workspace"
    large_workspace.mkdir(parents=True, exist_ok=True)
    for item in range(300):
        dependency = f"module_{item + 1}" if item < 299 else "module_0"
        (large_workspace / f"module_{item}.py").write_text(
            f"import {dependency}\ndef symbol_{item}(): return {item}\n",
            encoding="utf-8",
        )
    large_graph = RepositoryGraph(large_workspace)
    large_graph.build(max_files=500)

    catalog_sizes = [20, 50, 100, 200]
    exposure_counts = []
    exposure_deferred_counts = []
    low_pressure_counts = []
    low_pressure_deferred_counts = []
    visible_expected = [21, 51, 101, 201]
    low_pressure_deferred_expected = [0, 50, 100, 200]
    for size in catalog_sizes:
        catalog = ToolCatalog.from_specs(
            [_tool_spec("read_file"), *[_tool_spec(f"mcp_bench_{i}") for i in range(size)]],
        )
        pressure = ContextPressure(20_000, 15_000, 3_000)
        pressure_plan = ToolExposurePlanner(minimum_deferred_tools=1).plan(
            catalog, pressure=pressure,
        )
        exposure_counts.append(len(pressure_plan.visible_names))
        exposure_deferred_counts.append(len(pressure_plan.deferred_names))
        low_pressure_plan = ToolExposurePlanner(
            minimum_deferred_tools=1,
        ).plan(
            catalog,
            pressure=ContextPressure(200_000, 1_000, 8_000),
        )
        low_pressure_counts.append(len(low_pressure_plan.visible_names))
        low_pressure_deferred_counts.append(len(low_pressure_plan.deferred_names))
    discovery = _benchmark_tool_discovery()

    projector = ToolResultProjector(
        budget=ToolResultBudget(100),
        artifact_writer=lambda call_id, _output: f"artifacts/{call_id}.txt",
    )
    projected = projector.project_batch([
        (f"call-{i}", "bash", "head\n" + "x" * 4000 + "\nFAIL") for i in range(20)
    ], max_tokens=600)
    nominal_sla_turns = 15
    native_tool_turns = 40
    with scoped_runtime_overrides(nominal_agent_turns=nominal_sla_turns):
        native = run_native_long_horizon(workspace, tool_turns=native_tool_turns)
    nominal_sla_advisory = bool(
        native["model_calls"] > nominal_sla_turns
        and native["tool_results"] == native_tool_turns
        and native["result"].get("status") == "completed"
    )
    trace_events = list(native["events"])
    for turn in range(40):
        visible = projector.project_batch([
            (f"turn-{turn}", "read_file", f"turn {turn} evidence"),
        ], max_tokens=100)
        if visible[0].metadata["projected_tokens"] > 100:
            raise RuntimeError("Production projection exceeded the benchmark turn budget")

    verification_file = workspace / "verification_target.py"
    verification_file.write_text("def broken(:\n", encoding="utf-8")
    first_verify = subprocess.run(
        [sys.executable, "-m", "py_compile", str(verification_file)],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    trace_events.append({
        "event": "verification", "command": "python -m py_compile verification_target.py",
        "success": first_verify.returncode == 0,
    })
    verification_file.write_text("def fixed():\n    return True\n", encoding="utf-8")
    second_verify = subprocess.run(
        [sys.executable, "-m", "py_compile", str(verification_file)],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    trace_events.append({
        "event": "verification", "command": "python -m py_compile verification_target.py",
        "success": second_verify.returncode == 0,
    })

    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.agent.subagent import _new_subagent_state
    from nz_coder.runtime.worktree.manager import WorktreeManager
    conflict_workspace = root / "conflict-workspace"
    conflict_workspace.mkdir(parents=True, exist_ok=True)
    conflict_target = conflict_workspace / "app.py"
    conflict_target.write_text("base\n", encoding="utf-8")
    manager = BackgroundAgentManager(conflict_workspace, "benchmark-parent")
    baseline = manager._baseline(["app.py"])
    state = _new_subagent_state("benchmark-parent", "general-purpose", None)
    child_worktree = WorktreeManager(conflict_workspace).create(state["session_id"])
    child = Path(child_worktree.path)
    (child / "app.py").write_text("child\n", encoding="utf-8")
    state.update({
        "background": True, "status": "completed", "claimed_paths": ["app.py"],
        "changed_files": ["app.py"], "baseline_hashes": baseline,
        "worktree": {
            "id": child_worktree.id,
            "path": child_worktree.path,
            "branch": child_worktree.branch,
            "based_on": child_worktree.based_on,
            "head_commit": child_worktree.head_commit,
            "mode": child_worktree.mode,
        },
    })
    manager._save(state)
    conflict_target.write_text("parent changed\n", encoding="utf-8")
    _writes, _deletes, conflict_error = manager.application_changes(
        state["session_id"], ["app.py"],
    )
    trace_events.append({
        "event": "child_session", "conflicts": int(bool(conflict_error)),
        "detail": conflict_error,
    })
    trace_events.append({
        "event": "run_complete",
        "success": (
            second_verify.returncode == 0
            and nominal_sla_advisory
        ),
        "patch_valid": second_verify.returncode == 0,
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
    })
    trajectory_path = root / "trajectory.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    trajectory_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in trace_events),
        encoding="utf-8",
    )
    trajectory = AgentTrajectoryMetrics.from_jsonl(trajectory_path)
    diagnostics = diagnose_trajectory(trace_events)
    cases = {
        "A": {"passed": bool(index.search_symbols("normalize input", limit=1)),
              "located": (
                  index.search_symbols("normalize input", limit=1)[0]["path"]
                  if index.search_symbols("normalize input", limit=1) else ""
              )},
        "B": {"passed": len(index.callers("leaf")) == 1,
              "cross_file_callers": len(index.callers("leaf"))},
        "C": {"passed": large_graph.overview()["module_count"] == 300,
              "module_count": large_graph.overview()["module_count"]},
        "D": {"passed": (
                  exposure_counts == visible_expected
                  and exposure_deferred_counts == catalog_sizes
                  and low_pressure_counts == visible_expected
                  and low_pressure_deferred_counts == low_pressure_deferred_expected
                  and discovery["discovery_recall_hits"]
                  == discovery["discovery_recall_cases"]
                  and discovery["next_turn_unlocks"]
                  == discovery["discovery_recall_cases"]
                  and discovery["two_turn_token_savings_min_pct"] > 0
              ),
              "catalog_sizes": catalog_sizes, "visible_counts": exposure_counts,
              "hinted_counts": exposure_deferred_counts,
              "low_pressure_visible": low_pressure_counts,
              "low_pressure_hinted": low_pressure_deferred_counts,
              "schema_budget_enforced": (
                  low_pressure_deferred_counts == low_pressure_deferred_expected
              ), **discovery},
        "E": {"passed": sum(
            item.metadata["projected_tokens"] for item in projected
        ) <= 600, "aggregate_budget_respected": sum(
            item.metadata["projected_tokens"] for item in projected
        ) <= 600},
        "F": {"passed": nominal_sla_advisory, "turns": trajectory.turns,
              "long_horizon": trajectory.turns >= native_tool_turns,
              "nominal_sla_enforced": native["model_calls"] <= nominal_sla_turns,
              "nominal_sla_advisory": nominal_sla_advisory,
              "production_projection_calls": 40,
              "agent_runner_model_calls": native["model_calls"],
              "agent_runner_tool_results": native["tool_results"],
              "agent_runner_result": native["result"]},
        "G": {"passed": trajectory.verification_recoveries == 1,
              "verification_recovered": trajectory.verification_recoveries == 1,
              "first_exit_code": first_verify.returncode,
              "second_exit_code": second_verify.returncode},
        "H": {"passed": trajectory.conflicts == 1,
              "conflict_accounted": trajectory.conflicts == 1,
              "conflict_detail": conflict_error},
    }
    manifest_payload = [asdict(item) for item in benchmark_manifest()]
    result = {
        "version": 1,
        "suite_type": "core-capability-contract",
        "manifest_hash": hashlib.sha256(
            json.dumps(manifest_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "cases": cases,
        "trajectory_metrics": asdict(trajectory),
        "trajectory_diagnostics": asdict(diagnostics),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "core-capability-report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8",
    )
    return result


def _implementation_depth_evidence() -> dict:
    """Compute depth from inspectable production contracts, never a fixed rating."""
    import importlib.util
    from nz_coder.intelligence.analyzers import (
        AnalyzerRegistry, LanguageAnalyzer, PythonAstAnalyzer, TreeSitterAnalyzer,
        module_id_for_path,
    )
    from nz_coder.evaluation.behavioral import AgentBehaviorBenchmark
    from nz_coder.intelligence.code_index import (
        SCHEMA_VERSION, CallEdge, ReferenceEntry, SymbolEntry, UnresolvedCallTarget,
    )
    from nz_coder.intelligence.repository_graph import RepositoryGraph
    from nz_coder.intelligence.service import RepoIntelligenceService

    checks = {
        "identity_schema": SCHEMA_VERSION >= 3,
        "symbol_identity": "symbol_id" in SymbolEntry.__dataclass_fields__,
        "call_identity": "callee_symbol_id" in CallEdge.__dataclass_fields__,
        "reference_identity": "target_symbol_id" in ReferenceEntry.__dataclass_fields__,
        "analyzer_protocol": LanguageAnalyzer is not None,
        "python_ast": PythonAstAnalyzer().available(),
        "tree_sitter_adapter": hasattr(TreeSitterAnalyzer, "analyze_file"),
        "go_tree_sitter_adapter": "go" in TreeSitterAnalyzer.languages,
        "typescript_tree_sitter_runtime": (
            AnalyzerRegistry().capability_probe()["typescript"]["capability_tier"]
            == "tree-sitter"
        ),
        "javascript_tree_sitter_runtime": (
            AnalyzerRegistry().capability_probe()["javascript"]["capability_tier"]
            == "tree-sitter"
        ),
        "go_tree_sitter_runtime": (
            AnalyzerRegistry().capability_probe()["go"]["capability_tier"]
            == "tree-sitter"
        ),
        "native_watcher_available": importlib.util.find_spec("watchfiles") is not None,
        "workspace_runtime": hasattr(RepoIntelligenceService, "prewarm"),
        "generation_cache": hasattr(RepoIntelligenceService, "metrics"),
        "incremental_graph_api": hasattr(RepositoryGraph, "update_paths"),
        "unresolved_call_model": "candidates" in UnresolvedCallTarget.__dataclass_fields__,
        "agent_behavior_suite": hasattr(AgentBehaviorBenchmark, "run_matrix"),
        "bounded_lsp_augmentation": hasattr(RepoIntelligenceService, "augment_with_lsp"),
        "package_area_module_identity": (
            module_id_for_path("pkg/a.py") == module_id_for_path("pkg/b.py")
        ),
        "src_module_boundary": (
            module_id_for_path("src/auth/api.py")
            != module_id_for_path("src/payment/api.py")
        ),
        "unified_structural_lookup": hasattr(RepoIntelligenceService, "intent_lookup"),
        "trace_metrics_sink": hasattr(RepoIntelligenceService, "attach_tracer"),
    }
    passed = sum(bool(value) for value in checks.values())
    return {
        "structural_probe_pass_rate": round(100 * passed / max(1, len(checks)), 1),
        "passed": passed,
        "total": len(checks),
        "basis": f"{passed}/{len(checks)} inspectable depth checks passed",
        "scope": "structural/runtime depth probes, not an overall behavioral rating",
        "evidence": checks,
    }


def build_capability_report(outcomes: dict | None) -> dict:
    """Keep coverage, implementation depth, and observed behavior distinct."""
    report = {
        "feature_coverage": {
            "score": round(100 * len(benchmark_manifest()) / 8, 1),
            "basis": f"{len(benchmark_manifest())}/8 contract scenarios implemented",
        },
        "implementation_depth": _implementation_depth_evidence(),
    }
    if outcomes is None:
        report["behavioral_effectiveness"] = {"score": "unknown", "reason": "benchmark not run"}
    elif (
        str(outcomes.get("suite_type") or "").startswith("agent-behavior-production")
        and outcomes.get("evidence_kind") == "production"
    ):
        runs = list(outcomes.get("runs") or ())
        passed = sum(bool(run.get("score", {}).get("success")) for run in runs)
        report["behavioral_effectiveness"] = {
            "score": round(100 * passed / max(1, len(runs)), 1),
            "basis": f"{passed}/{len(runs)} Agent-executed behavior runs succeeded",
        }
    elif str(outcomes.get("suite_type") or "").startswith("agent-behavior-controlled"):
        runs = list(outcomes.get("runs") or ())
        passed = sum(bool(run.get("score", {}).get("success")) for run in runs)
        report["behavioral_effectiveness"] = {
            "score": "unknown",
            "reason": (
                "controlled Agent trajectories validate integration mechanics, "
                "not real-model behavioral effectiveness"
            ),
            "controlled_success_rate": round(100 * passed / max(1, len(runs)), 1),
        }
    else:
        cases = outcomes.get("cases", {})
        passed = sum(bool(case.get("passed")) for case in cases.values())
        report["behavioral_effectiveness"] = {
            "score": "unknown",
            "reason": "contract evidence is not an Agent behavioral benchmark",
            "contract_pass_rate": round(100 * passed / max(1, len(cases)), 1),
        }
    return report


__all__ = [
    "AgentTrajectoryMetrics", "BenchmarkCase", "CoreCapabilityContractSuite",
    "benchmark_manifest",
    "TrajectoryDiagnostics", "build_capability_report", "diagnose_trajectory",
    "run_local_benchmark",
]
