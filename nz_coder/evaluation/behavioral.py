"""Agent-executed behavioral benchmark for coding capability.

The harness creates fixtures and scores observable outputs.  It never performs
the task's edits or recovery steps.  Drivers may wrap a production AgentRunner
with either a real coding model or a deterministic controllable model.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import difflib
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
from typing import Callable, Protocol

from nz_coder.evaluation.core_capability import (
    AgentTrajectoryMetrics,
    diagnose_trajectory,
)
from nz_coder.evaluation.native_scenario import run_native_agent_scenario
from nz_coder.intelligence.service import RepoIntelligenceService
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.execution.runner import AgentRunner


@dataclass(frozen=True)
class BehaviorBenchmarkConfig:
    model: str
    provider: str = ""
    reasoning: str = "medium"
    temperature: float = 0.0
    max_turns: int = 40
    token_budget: int = 100_000
    cost_budget: float | None = None
    repo_intelligence: str = "v3"
    tool_catalog_size: int = 50
    progressive_exposure: bool = True
    child_agents: int = 1
    repetition: int = 1
    retrieval_strategy: str = "guidance"
    semantic_model: str = ""
    web_search_enabled: bool = False


@dataclass(frozen=True)
class BehaviorTask:
    case_id: str
    capability: str
    prompt: str
    expected_files: tuple[str, ...] = ()
    expected_symbols: tuple[str, ...] = ()
    expected_call_path: tuple[str, ...] = ()
    verification_command: tuple[str, ...] = ()
    min_turns: int = 1
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BehaviorObservation:
    final_response: str
    events: tuple[dict, ...] = ()
    run_result: dict = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()
    error: str = ""


class BehaviorDriver(Protocol):
    """Execution boundary: the driver, not the scorer, operates the Agent."""

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation: ...


class AgentRunnerBehaviorDriver:
    """Adapt a fully composed production AgentRunner to benchmark tasks."""

    evidence_kind = "production"

    def __init__(
        self,
        runner: AgentRunner,
        request_factory: Callable[[BehaviorTask, Path, BehaviorBenchmarkConfig], RunRequest],
        *,
        event_loader: Callable[[RunRequest, dict], list[dict]] | None = None,
        changed_files: Callable[[Path], list[str]] | None = None,
    ) -> None:
        self.runner = runner
        self.request_factory = request_factory
        self.event_loader = event_loader or (lambda _request, _result: [])
        self.changed_files = changed_files or (lambda _workspace: [])

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation:
        request = self.request_factory(task, workspace, config)
        result = asyncio.run(self.runner.run(
            request, options=RunOptions(stream=False),
        ))
        return BehaviorObservation(
            final_response=str(result.get("content") or ""),
            events=tuple(self.event_loader(request, result)),
            run_result=dict(result),
            changed_files=tuple(self.changed_files(workspace)),
        )


class ProductionAgentBehaviorDriver:
    """Run benchmark tasks through the same composed runtime as SDK/CLI users."""

    evidence_kind = "production"

    _CORE_TOOLS = (
        "tool_search", "todo", "read_file", "write_file", "write_files_batch",
        "edit_file", "apply_patch", "replace_lines", "list_directory", "bash",
        "grep_search", "glob_search", "diff_status", "verify_changed_files",
        "read_symbol", "find_symbol_callers",
    )
    _CURRENT_REPO_TOOLS = ("repo_map", "code_references")
    _V3_REPO_TOOLS = ("repo_context", "repo_map", "code_references", "analyze_impact")

    @staticmethod
    def _dynamic_tools(
        task: BehaviorTask, config: BehaviorBenchmarkConfig,
    ) -> list[dict]:
        if task.case_id != "H" or config.tool_catalog_size <= 3:
            return []
        count = config.tool_catalog_size - 3

        def noop(query: str = "") -> str:
            return f"benchmark distractor has no repository data for {query!r}"

        padding = " Bounded synthetic catalog entry used only for tool-selection measurement."
        return [{
            "name": f"mcp_benchmark_distractor_{index:03d}",
            "description": f"Inspect unrelated benchmark resource {index}." + padding * 5,
            "parameters": {
                "type": "object", "properties": {"query": {"type": "string"}},
            },
            "handler": noop, "execution": "read",
        } for index in range(max(0, count))]

    @classmethod
    def _tool_names(
        cls, task: BehaviorTask, config: BehaviorBenchmarkConfig,
        dynamic_names: list[str],
    ) -> tuple[str, ...]:
        names = list(cls._CORE_TOOLS)
        if config.repo_intelligence == "current":
            names.extend(cls._CURRENT_REPO_TOOLS)
        elif config.repo_intelligence in {"v3", "lookup"}:
            names.extend(cls._V3_REPO_TOOLS)
        elif config.repo_intelligence != "off":
            raise ValueError("repo_intelligence must be off, current, v3, or lookup")
        if config.semantic_model:
            names.append("semantic_search")
        if task.case_id.startswith("W"):
            names.append("webfetch")
            if config.web_search_enabled:
                names.append("web_search")
        if task.case_id.startswith("P"):
            names.append("process")
        if task.case_id == "G" and config.child_agents > 1:
            names.extend(("task", "agent_manager", "apply_agent_changes"))
        names.extend(dynamic_names)
        # H is the only catalog-size experiment; other cases retain their full
        # task tool surface regardless of the H matrix setting.
        if task.case_id == "H":
            useful = ["tool_search", "read_file", "grep_search", "repo_context"]
            remaining = max(0, config.tool_catalog_size - len(useful))
            names = [*useful, *dynamic_names[:remaining]]
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _trace_events(workspace: Path, session_id: str) -> list[dict]:
        root = workspace / ".nz-coder" / "sessions" / "_artifacts" / session_id
        events: list[dict] = []
        for path in sorted(root.rglob("*.jsonl")) if root.exists() else ():
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return events

    @staticmethod
    def _normalize_events(events: list[dict], schema_tokens: int) -> list[dict]:
        result: list[dict] = []
        for event in events:
            value = dict(event)
            if value.get("event") == "llm_response":
                value["schema_tokens"] = schema_tokens
            result.append(value)
            if value.get("event") != "tool_call" or value.get("name") != "bash":
                continue
            arguments = value.get("input")
            command = str(arguments.get("command") or "") if isinstance(arguments, dict) else ""
            if not any(token in command for token in ("pytest", " test", "test ", "unittest")):
                continue
            result.append({
                "event": "verification", "command": command,
                "success": value.get("status") == "ok",
            })
        return result

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation:
        from nz_coder.runtime.core.profiles import MAIN_PROFILE
        from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
        from nz_coder.sdk import AgentClient
        from nz_coder.tools import get_specs, scoped_dynamic_tools
        from nz_coder.tool_platform.catalog import ToolCatalog
        from nz_coder.tool_platform.exposure import ContextPressure, ToolExposurePlanner
        from nz_coder.runtime.core.request import AgentDefinition

        definitions = self._dynamic_tools(task, config)
        effort = (
            config.reasoning
            if config.reasoning not in {"", "none", "provider-default"}
            else None
        )
        session_id = f"behavior-{task.case_id}-{time.time_ns()}"
        with scoped_dynamic_tools(definitions) as dynamic_names:
            tool_names = self._tool_names(task, config, dynamic_names)
            selected = [
                spec for spec in get_specs()
                if str(spec.get("function", {}).get("name") or "") in set(tool_names)
            ]
            catalog = ToolCatalog.from_specs(selected)
            pressure = ContextPressure(
                context_window=8_000 if config.progressive_exposure else 1_000_000,
                used_tokens=5_000 if config.progressive_exposure else 0,
                reserve_tokens=2_000 if config.progressive_exposure else 0,
            )
            exposure = ToolExposurePlanner().plan(catalog, pressure=pressure)
            schema_tokens = (
                exposure.estimated_tokens_after
                if config.progressive_exposure else exposure.estimated_tokens_before
            )
            child_instruction = (
                f" Use at most {max(0, config.child_agents - 1)} child agents and integrate their work."
                if task.case_id == "G" else ""
            )
            prompt = task.prompt + child_instruction
            request = RunRequest(
                agent=AgentDefinition(
                    name="behavior-benchmark",
                    instructions=(
                        "Solve the repository task yourself using the provided tools. "
                        "Inspect evidence before answering, make requested edits, run verification, "
                        "recover from failures, and report concrete files and symbols."
                    ),
                    allowed_tools=tool_names,
                    provider=config.provider or None, model=config.model or None,
                    reasoning_effort=effort,
                ),
                profile=MAIN_PROFILE,
                messages=({"role": "user", "content": prompt},),
                workspace=workspace, session_id=session_id, tool_names=tool_names,
                stream=False, provider=config.provider or None,
                model=config.model or None, reasoning_effort=effort,
                metadata={
                    "permission_mode": "auto", "persist_session": False,
                    "behavior_benchmark": asdict(config),
                    "context_pressure": asdict(pressure),
                    "model_capability_options": {
                        "temperature": float(config.temperature),
                    },
                    "repo_intelligence_mode": config.repo_intelligence,
                    "repo_retrieval_strategy": config.retrieval_strategy,
                    "semantic_model": config.semantic_model,
                },
            )

            async def execute():
                return await AgentClient().run(request)

            with scoped_runtime_overrides(
                max_agent_turns=config.max_turns,
                max_parallel_tasks=max(1, config.child_agents - 1),
                strict_local_tools=not task.case_id.startswith("P"),
                repo_intelligence_mode=config.repo_intelligence,
                repo_retrieval_strategy=config.retrieval_strategy,
            ):
                run_result = asyncio.run(execute())

        events = self._normalize_events(
            self._trace_events(workspace, session_id), schema_tokens,
        )
        status = getattr(run_result.status, "value", str(run_result.status))
        events.append({
            "event": "run_complete", "success": status == "completed",
            "patch_valid": True,
        })
        usage = run_result.usage
        runtime_error = run_result.error
        raw_status = str(run_result.metadata.get("raw_status") or "")
        if not runtime_error and status != "completed" and raw_status != "completed_unverified":
            runtime_error = f"agent run ended with status={status} raw_status={raw_status or 'unknown'}"
        payload = {
            "status": status, "content": run_result.final_text,
            "session_id": run_result.session_id, "active_agent": run_result.active_agent,
            "error": runtime_error, "metadata": dict(run_result.metadata),
            "usage": asdict(usage), "schema_tokens_per_turn": schema_tokens,
            "visible_tools": len(exposure.visible_names),
            "deferred_tools": len(exposure.deferred_names),
        }
        return BehaviorObservation(
            final_response=run_result.final_text, events=tuple(events),
            run_result=payload,
            changed_files=tuple(run_result.metadata.get("changed_files") or ()),
            error=runtime_error,
        )


class CallableBehaviorDriver:
    """Adapter for a deterministic controllable model used in CI."""

    def __init__(self, callback: Callable) -> None:
        self.callback = callback
        self.evidence_kind = "controlled"

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation:
        return self.callback(task, workspace, config)


def behavior_manifest() -> tuple[tuple[str, str], ...]:
    return (
        ("A", "unknown-location-localization"),
        ("B", "cross-file-impact"),
        ("C", "process-understanding"),
        ("D", "large-repo-navigation"),
        ("E", "long-horizon"),
        ("F", "verification-recovery"),
        ("G", "multi-agent"),
        ("H", "tool-scale"),
        ("I", "vocabulary-mismatch-semantic-localization"),
        ("I2", "failed-settlement-requeue-vocabulary-mismatch"),
        ("I3", "detached-peer-work-restoration-vocabulary-mismatch"),
        ("I4", "expired-access-snapshot-vocabulary-mismatch"),
        ("IS", "short-business-intent-semantic-localization"),
    )


def verification_behavior_manifest() -> tuple[tuple[str, str], ...]:
    return (
        ("V1", "syntax-failure-recovery"),
        ("V2", "targeted-test-recovery"),
        ("V3", "regression-failure-recovery"),
        ("V4", "correct-patch-no-tests"),
        ("V5", "correct-cross-file-rename"),
        ("V6", "environment-failure-classification"),
        ("V7", "repeated-failure-stall-recovery"),
        ("V8", "partial-patch-impact-recovery"),
        ("C1", "premature-completion-blocked"),
        ("C2", "passed-evidence-completes"),
        ("C3", "static-evidence-no-tests"),
        ("C4", "environment-degraded-completion"),
    )


def web_search_behavior_manifest() -> tuple[tuple[str, str], ...]:
    return (
        ("W1", "latest-library-api"),
        ("W2", "breaking-change-migration"),
        ("W3", "obscure-compiler-error"),
        ("W4", "github-issue-workaround"),
        ("W5", "local-only-no-web"),
    )


def process_behavior_manifest() -> tuple[tuple[str, str], ...]:
    return (
        ("P1", "persistent-dev-server"),
        ("P2", "persistent-watch-mode"),
        ("P3", "persistent-repl"),
        ("P4", "persistent-log-cursor"),
        ("P5", "persistent-process-crash"),
        ("P6", "multiple-persistent-processes"),
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_a(root: Path) -> BehaviorTask:
    _write(root / "app/routes/session.py", (
        "from auth.tokens import refresh_if_expired\n\n"
        "def session_route(token):\n    return refresh_if_expired(token)\n"
    ))
    _write(root / "auth/tokens.py", (
        "from auth.client import rotate_token\n\n"
        "def refresh_if_expired(token):\n"
        "    if token.expired:\n        return rotate_token(token.refresh_token)\n"
        "    return token\n"
    ))
    _write(root / "auth/client.py", (
        "def rotate_token(refresh_token):\n"
        "    return {'access_token': 'new', 'refresh_token': refresh_token}\n"
    ))
    _write(root / "billing/tokens.py", (
        "def refresh_if_expired(invoice):\n    return invoice.refresh_totals()\n"
    ))
    _write(root / "tests/test_token_refresh.py", (
        "from auth.tokens import refresh_if_expired\n\n"
        "def test_refreshes_expired_token():\n    assert callable(refresh_if_expired)\n"
    ))
    return BehaviorTask(
        "A", "unknown-location-localization",
        "Find the code responsible for automatically refreshing an expired user token, "
        "explain the call chain, and identify the locations that should be modified. "
        "No file or symbol names are provided.",
        ("app/routes/session.py", "auth/tokens.py", "auth/client.py"),
        ("session_route", "refresh_if_expired", "rotate_token"),
        ("session_route", "refresh_if_expired", "rotate_token"),
    )


def _fixture_b(root: Path) -> BehaviorTask:
    _write(root / "catalog/api.py", "def format_product(product):\n    return product['name']\n")
    _write(root / "web/controller.py", "from catalog.api import format_product\ndef show(p): return format_product(p)\n")
    _write(root / "cli/report.py", "from catalog.api import format_product\ndef report(p): return format_product(p)\n")
    _write(root / "mobile/presenter.py", "from catalog.api import format_product\ndef title(p): return format_product(p)\n")
    _write(root / "jobs/export.py", "from web.controller import show\ndef export(p): return show(p)\n")
    _write(root / "tests/test_catalog_api.py", "from catalog.api import format_product\ndef test_format(): assert format_product({'name':'A'}) == 'A'\n")
    _write(root / "tests/test_report.py", "from catalog.api import format_product\ndef test_report(): assert format_product({'name':'A'}) == 'A'\n")
    return BehaviorTask(
        "B", "cross-file-impact",
        "Rename the public product formatting API to render_product and update every "
        "affected caller and test. Verify the complete change.",
        ("catalog/api.py", "web/controller.py", "cli/report.py", "mobile/presenter.py", "jobs/export.py",
         "tests/test_catalog_api.py", "tests/test_report.py"),
        ("format_product", "render_product"), verification_command=(
            "python", "-m", "pytest", "-q",
        ),
        metadata={
            "required_text": "render_product", "forbidden_text": "format_product",
            "must_change": [
                "catalog/api.py", "web/controller.py", "cli/report.py", "mobile/presenter.py",
                "tests/test_catalog_api.py", "tests/test_report.py",
            ],
        },
    )


def _fixture_c(root: Path) -> BehaviorTask:
    _write(root / "http/routes.py", "from app.controller import create_user\ndef post_user(req): return create_user(req.body)\n")
    _write(root / "app/controller.py", "from domain.service import register_user\ndef create_user(data): return register_user(data)\n")
    _write(root / "domain/service.py", "from storage.repository import save_user\ndef register_user(data): return save_user(data)\n")
    _write(root / "storage/repository.py", "from storage.client import insert\ndef save_user(data): return insert('users', data)\n")
    _write(root / "storage/client.py", "def insert(table, data): return {'table': table, **data}\n")
    return BehaviorTask(
        "C", "process-understanding",
        "Explain the complete request path from the HTTP entrypoint to persistence.",
        tuple(str(path) for path in (
            "http/routes.py", "app/controller.py", "domain/service.py",
            "storage/repository.py", "storage/client.py",
        )),
        ("post_user", "create_user", "register_user", "save_user", "insert"),
        ("post_user", "create_user", "register_user", "save_user", "insert"),
    )


def _fixture_d(root: Path) -> BehaviorTask:
    for index in range(500):
        _write(root / f"packages/pkg_{index}/module.py", f"def unrelated_{index}(): return {index}\n")
    _write(root / "platform/retry/policy.py", "def retry_failed_payment(attempt): return attempt < 3\n")
    _write(root / "platform/retry/worker.py", "from platform.retry.policy import retry_failed_payment\ndef process(attempt): return retry_failed_payment(attempt)\n")
    return BehaviorTask(
        "D", "large-repo-navigation",
        "Locate and explain the failed-payment retry policy without being given a file name.",
        ("platform/retry/policy.py", "platform/retry/worker.py"),
        ("retry_failed_payment", "process"),
    )


def _fixture_e(root: Path) -> BehaviorTask:
    task = _fixture_b(root)
    _write(root / "tests/test_edge_case.py", (
        "from catalog.api import format_product\n"
        "def test_missing_name(): assert format_product({'name': ''}) == ''\n"
    ))
    return BehaviorTask(
        "E", "long-horizon",
        task.prompt + " Also preserve empty-name behavior and recover from any test failure.",
        (*task.expected_files, "tests/test_edge_case.py"), task.expected_symbols,
        verification_command=task.verification_command, min_turns=15,
        metadata={
            **task.metadata,
            "must_change": [
                "catalog/api.py", "web/controller.py", "cli/report.py", "mobile/presenter.py",
                "tests/test_catalog_api.py", "tests/test_report.py",
                "tests/test_edge_case.py",
            ],
        },
    )


def _fixture_f(root: Path) -> BehaviorTask:
    _write(root / "calc/service.py", "def ratio(total, count):\n    return total / count\n")
    _write(root / "tests/test_ratio.py", (
        "from calc.service import ratio\n"
        "def test_empty_count(): assert ratio(10, 0) == 0\n"
    ))
    return BehaviorTask(
        "F", "verification-recovery",
        "Fix the ratio behavior for an empty count. Run tests, diagnose failures, "
        "repair the implementation, and verify again.",
        ("calc/service.py", "tests/test_ratio.py"), ("ratio",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={
            "required_text": "count", "must_change": ["calc/service.py"],
            "requires_verification_recovery": True,
        },
    )


def _fixture_g(root: Path) -> BehaviorTask:
    for name in ("users", "orders", "billing", "search"):
        _write(root / f"{name}/api.py", f"def {name}_health(): return 'ok'\n")
        _write(root / f"tests/test_{name}.py", f"from {name}.api import {name}_health\ndef test_health(): assert {name}_health() == 'ok'\n")
    return BehaviorTask(
        "G", "multi-agent",
        "Add a status constant to each of the four service APIs and update their tests. "
        "The service workstreams may be performed in parallel.",
        tuple(
            path for name in ("users", "orders", "billing", "search")
            for path in (f"{name}/api.py", f"tests/test_{name}.py")
        ),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={
            "required_text": "STATUS",
            "must_change": [
                path for name in ("users", "orders", "billing", "search")
                for path in (f"{name}/api.py", f"tests/test_{name}.py")
            ],
        },
    )


def _fixture_h(root: Path) -> BehaviorTask:
    _write(root / "app.py", "def normalize_email(value): return value.strip().lower()\n")
    return BehaviorTask(
        "H", "tool-scale",
        "Find and explain the email normalization behavior using the available tools.",
        ("app.py",), ("normalize_email",),
    )


def _fixture_i(root: Path) -> BehaviorTask:
    for index in range(100):
        _write(
            root / f"components/area_{index:03d}/handler.py",
            f"def unrelated_{index}(value): return value\n",
        )
    _write(root / "gateway/checkout.py", (
        "from workflow.coordinator import close_cart\n"
        "def finalize_cart(cart): return close_cart(cart)\n"
    ))
    _write(root / "workflow/coordinator.py", (
        "from archive.store import commit_record\n"
        "from messaging.outbound import dispatch_receipt\n"
        "def close_cart(cart):\n"
        "    record = commit_record(cart)\n"
        "    dispatch_receipt(record)\n"
        "    return record\n"
    ))
    _write(root / "archive/store.py", (
        "def commit_record(cart): return {'id': cart['id'], 'state': 'closed'}\n"
    ))
    _write(root / "messaging/outbound.py", (
        "def dispatch_receipt(record): return {'accepted': record['id']}\n"
    ))
    return BehaviorTask(
        "I", "unknown-location-localization",
        "After a customer has paid, find how the transaction is durably retained and "
        "how the customer is notified. Explain the complete path. No file or symbol "
        "names are provided, and the implementation uses different domain vocabulary.",
        (
            "gateway/checkout.py", "workflow/coordinator.py",
            "archive/store.py", "messaging/outbound.py",
        ),
        ("finalize_cart", "close_cart", "commit_record", "dispatch_receipt"),
        ("finalize_cart", "close_cart", "commit_record", "dispatch_receipt"),
    )


def _fixture_i2(root: Path) -> BehaviorTask:
    for index in range(80):
        _write(root / f"areas/unit_{index:03d}/logic.py", f"def task_{index}(x): return x\n")
    _write(root / "settlement/decline.py", (
        "from scheduling.allowance import permit_another_attempt\n"
        "def handle_decline(history): return permit_another_attempt(history)\n"
    ))
    _write(root / "scheduling/allowance.py", (
        "def permit_another_attempt(history): return len(history) < 3\n"
    ))
    return BehaviorTask(
        "I2", "unknown-location-localization",
        "Where is failed payment retry throttling handled? Explain the path. No file "
        "or symbol names are provided and the implementation uses different vocabulary.",
        ("settlement/decline.py", "scheduling/allowance.py"),
        ("handle_decline", "permit_another_attempt"),
        ("handle_decline", "permit_another_attempt"),
    )


def _fixture_i3(root: Path) -> BehaviorTask:
    for index in range(80):
        _write(root / f"features/part_{index:03d}/service.py", f"def action_{index}(x): return x\n")
    _write(root / "transport/peer_state.py", (
        "from journal.playback import replay_queue\n"
        "def restore_peer(peer_id): return replay_queue(peer_id)\n"
    ))
    _write(root / "journal/playback.py", (
        "def replay_queue(peer_id): return {'peer': peer_id, 'state': 'continued'}\n"
    ))
    return BehaviorTask(
        "I3", "unknown-location-localization",
        "Find how disconnected clients resume unfinished work and explain the path. "
        "The implementation uses different domain vocabulary.",
        ("transport/peer_state.py", "journal/playback.py"),
        ("restore_peer", "replay_queue"),
        ("restore_peer", "replay_queue"),
    )


def _fixture_i4(root: Path) -> BehaviorTask:
    for index in range(80):
        _write(root / f"modules/segment_{index:03d}/rules.py", f"def rule_{index}(x): return x\n")
    _write(root / "access/session.py", (
        "from grants.snapshot import discard_expired_grants\n"
        "def refresh_principal(subject): return discard_expired_grants(subject)\n"
    ))
    _write(root / "grants/snapshot.py", (
        "def discard_expired_grants(subject): return {'subject': subject, 'grants': []}\n"
    ))
    return BehaviorTask(
        "I4", "unknown-location-localization",
        "Where is stale cached authorization invalidated? Explain the complete path. "
        "No implementation terms are provided.",
        ("access/session.py", "grants/snapshot.py"),
        ("refresh_principal", "discard_expired_grants"),
        ("refresh_principal", "discard_expired_grants"),
    )


def _fixture_is(root: Path) -> BehaviorTask:
    for index in range(80):
        _write(root / f"domains/slice_{index:03d}/unit.py", f"def unit_{index}(x): return x\n")
    _write(root / "ledger/intake.py", (
        "from ledger.reservation import reserve_once\n"
        "def accept_document(document): return reserve_once(document)\n"
    ))
    _write(root / "ledger/reservation.py", (
        "def reserve_once(document): return {'key': document['id'], 'attempts': 1}\n"
    ))
    return BehaviorTask(
        "IS", "unknown-location-localization",
        "fix duplicate invoice retries",
        ("ledger/intake.py", "ledger/reservation.py"),
        ("accept_document", "reserve_once"),
        ("accept_document", "reserve_once"),
    )


def _fixture_v1(root: Path) -> BehaviorTask:
    _write(root / "app/parser.py", "def parse_total(value)\n    return int(value)\n")
    _write(root / "tests/test_parser.py", "from app.parser import parse_total\ndef test_total(): assert parse_total('4') == 4\n")
    return BehaviorTask(
        "V1", "verification-reliability",
        "First run the focused parser test to capture the current syntax failure. Then fix it, rerun verification, and finish only after it passes.",
        ("app/parser.py", "tests/test_parser.py"), ("parse_total",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={"required_text": "def parse_total(value):", "must_change": ["app/parser.py"], "requires_verification_recovery": True},
    )


def _fixture_v2(root: Path) -> BehaviorTask:
    _write(root / "billing/retry.py", "def retry_delay(attempt):\n    return attempt\n")
    _write(root / "tests/test_retry.py", "from billing.retry import retry_delay\ndef test_backoff(): assert retry_delay(3) == 8\n")
    return BehaviorTask(
        "V2", "verification-reliability",
        "First run the targeted backoff test to capture the current failure. Then fix retry_delay from that evidence and rerun it.",
        ("billing/retry.py", "tests/test_retry.py"), ("retry_delay",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={"required_text": "2 ** attempt", "must_change": ["billing/retry.py"], "requires_verification_recovery": True},
    )


def _fixture_v3(root: Path) -> BehaviorTask:
    _write(root / "names/normalize.py", "def normalize(value):\n    return value.strip().lower()\n")
    _write(root / "tests/test_normalize.py", "from names.normalize import normalize\ndef test_basic(): assert normalize(' A ') == 'a'\n")
    _write(root / "tests/test_slug_contract.py", "from names.normalize import normalize\ndef test_separator_contract(): assert normalize('A B') == 'a-b'\n")
    return BehaviorTask(
        "V3", "verification-reliability",
        "First run the current test suite. Then update name normalization to produce URL-safe words, verify the focused test and dependent slug contract, and recover from the regression evidence.",
        ("names/normalize.py", "tests/test_normalize.py", "tests/test_slug_contract.py"), ("normalize",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={"must_change": ["names/normalize.py"], "requires_verification_recovery": True},
    )


def _fixture_v4(root: Path) -> BehaviorTask:
    _write(root / "util/labels.py", "def label(value):\n    return value.strip()\n")
    return BehaviorTask(
        "V4", "verification-reliability",
        "Make label return uppercase text. This repository has no tests; use appropriate static evidence and report that limitation honestly.",
        ("util/labels.py",), ("label",),
        verification_command=("python", "-m", "py_compile", "util/labels.py"),
        metadata={"required_text": "upper", "must_change": ["util/labels.py"]},
    )


def _fixture_v5(root: Path) -> BehaviorTask:
    task = _fixture_b(root)
    return BehaviorTask("V5", "verification-reliability", task.prompt, task.expected_files,
                        task.expected_symbols, task.expected_call_path,
                        task.verification_command, task.min_turns, task.metadata)


def _fixture_v6(root: Path) -> BehaviorTask:
    _write(root / "config/limits.py", "MAX_RETRIES = 2\n")
    _write(root / "conftest.py", "pytest_plugins = ['missing_benchmark_plugin']\n")
    return BehaviorTask(
        "V6", "verification-reliability",
        "Change MAX_RETRIES to 3. Run pytest, whose required plugin is unavailable here; distinguish that environment failure from code failure, run an available static check, and finish with honest degraded verification. Do not modify conftest.py.",
        ("config/limits.py",), ("MAX_RETRIES",),
        verification_command=("python", "-m", "py_compile", "config/limits.py"),
        metadata={"required_text": "MAX_RETRIES = 3", "must_change": ["config/limits.py"], "expected_degraded": True},
    )


def _fixture_v7(root: Path) -> BehaviorTask:
    _write(root / "job_queue/worker.py", "def pending(items):\n    return len(items) - 1\n")
    _write(root / "tests/test_worker.py", "from job_queue.worker import pending\ndef test_empty(): assert pending([]) == 0\n")
    return BehaviorTask(
        "V7", "verification-reliability",
        "First run the empty-queue test. Then fix pending from its failure evidence; do not repeat an unchanged failing command, and verify after the edit.",
        ("job_queue/worker.py", "tests/test_worker.py"), ("pending",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={"required_text": "max", "must_change": ["job_queue/worker.py"], "requires_verification_recovery": True},
    )


def _fixture_v8(root: Path) -> BehaviorTask:
    _write(root / "public/api.py", "def encode_v1(value): return f'v2:{value}'\n")
    _write(root / "client/a.py", "from public.api import encode_v1\ndef send(v): return encode_v1(v)\n")
    _write(root / "client/b.py", "from public.api import encode_v1\ndef store(v): return encode_v1(v)\n")
    _write(root / "tests/test_clients.py", "from client.a import send\nfrom client.b import store\ndef test_clients(): assert send('x') == store('x') == 'v2:x'\n")
    return BehaviorTask(
        "V8", "verification-reliability",
        "First run the client test. Rename public encode_v1 to encode_v2 and update every affected caller. Use impact evidence and tests to catch any partial patch.",
        ("public/api.py", "client/a.py", "client/b.py", "tests/test_clients.py"), ("encode_v1", "encode_v2"),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={"required_text": "encode_v2", "forbidden_text": "encode_v1", "must_change": ["public/api.py", "client/a.py", "client/b.py"]},
    )


def _fixture_c1(root: Path) -> BehaviorTask:
    task = _fixture_v2(root)
    return BehaviorTask("C1", "completion-correctness", task.prompt + " Do not claim completion while the test fails.",
                        task.expected_files, task.expected_symbols, task.expected_call_path,
                        task.verification_command, task.min_turns, task.metadata)


def _fixture_c2(root: Path) -> BehaviorTask:
    _write(root / "maths/add.py", "def add(a, b): return a - b\n")
    _write(root / "tests/test_add.py", "from maths.add import add\ndef test_add(): assert add(2, 3) == 5\n")
    return BehaviorTask("C2", "completion-correctness", "Fix add and finish once the focused evidence passes.",
                        ("maths/add.py", "tests/test_add.py"), ("add",),
                        verification_command=("python", "-m", "pytest", "-q"),
                        metadata={"required_text": "a + b", "must_change": ["maths/add.py"]})


def _fixture_c3(root: Path) -> BehaviorTask:
    task = _fixture_v4(root)
    return BehaviorTask("C3", "completion-correctness", task.prompt, task.expected_files,
                        task.expected_symbols, task.expected_call_path,
                        task.verification_command, task.min_turns, task.metadata)


def _fixture_c4(root: Path) -> BehaviorTask:
    task = _fixture_v6(root)
    return BehaviorTask("C4", "completion-correctness", task.prompt, task.expected_files,
                        task.expected_symbols, task.expected_call_path,
                        task.verification_command, task.min_turns, task.metadata)


def _web_research_task(
    root: Path,
    case_id: str,
    prompt: str,
    *,
    required_terms: tuple[str, ...],
    source_domains: tuple[str, ...],
) -> BehaviorTask:
    _write(root / "README.md", "# External compatibility research fixture\n")
    return BehaviorTask(
        case_id, "web-knowledge", prompt + (
            " Verify the answer against a primary source using the available web tools. "
            "Give a concise conclusion and the exact source URL in your final answer. Do not modify files."
        ),
        metadata={
            "response_evidence": True,
            "required_terms": list(required_terms),
            "source_domains": list(source_domains),
        },
    )


def _fixture_w1(root: Path) -> BehaviorTask:
    return _web_research_task(
        root, "W1",
        "Determine whether Python 3.14 pathlib.Path.copy exists and state what it returns.",
        required_terms=("Path.copy", "Path"),
        source_domains=("docs.python.org",),
    )


def _fixture_w2(root: Path) -> BehaviorTask:
    return _web_research_task(
        root, "W2",
        "For a Pydantic V1 to V2 migration, determine where BaseSettings moved.",
        required_terms=("BaseSettings", "pydantic-settings"),
        source_domains=("docs.pydantic.dev", "pydantic.dev"),
    )


def _fixture_w3(root: Path) -> BehaviorTask:
    return _web_research_task(
        root, "W3",
        "Investigate TypeScript diagnostic TS1479 and explain the CommonJS/ESM mismatch it reports.",
        required_terms=("TS1479", "CommonJS", "ECMAScript"),
        source_domains=("github.com", "typescriptlang.org"),
    )


def _fixture_w4(root: Path) -> BehaviorTask:
    return _web_research_task(
        root, "W4",
        "Find a public GitHub issue explaining why require('node-fetch') can raise ERR_REQUIRE_ESM with node-fetch v3 and give the supported migration choices.",
        required_terms=("ERR_REQUIRE_ESM", "node-fetch", "ESM"),
        source_domains=("github.com",),
    )


def _fixture_w5(root: Path) -> BehaviorTask:
    _write(root / "app/config.py", "API_TIMEOUT = 5\n")
    _write(root / "tests/test_config.py", "from app.config import API_TIMEOUT\ndef test_timeout(): assert API_TIMEOUT == 10\n")
    return BehaviorTask(
        "W5", "local-only-no-web",
        "Update the local API timeout to 10 and verify the focused test. This task needs no external research.",
        ("app/config.py",), ("API_TIMEOUT",),
        verification_command=("python", "-m", "pytest", "-q"),
        metadata={
            "required_text": "API_TIMEOUT = 10", "must_change": ["app/config.py"],
            "forbid_web_search": True,
        },
    )


def _process_task(
    root: Path,
    case_id: str,
    prompt: str,
    *,
    scripts: dict[str, str],
    required_operations: dict[str, int],
    expected_output_terms: tuple[str, ...],
    min_processes: int = 1,
    require_cursor_reuse: bool = False,
    expected_exit_code: int | None = None,
) -> BehaviorTask:
    for path, content in scripts.items():
        _write(root / path, content)
    return BehaviorTask(
        case_id,
        "persistent-process",
        prompt,
        metadata={
            "required_process_operations": required_operations,
            "expected_process_output_terms": list(expected_output_terms),
            "min_distinct_processes": min_processes,
            "require_cursor_reuse": require_cursor_reuse,
            "expected_process_exit_code": expected_exit_code,
            "require_zero_orphans": True,
        },
    )


def _fixture_p1(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P1",
        "Use the persistent process tool to start `python -u fixture_server.py`, "
        "wait until its logs say SERVER_READY, then in a later tool turn use bash "
        "to request http://127.0.0.1:8765 and confirm the body is HTTP_OK. Read "
        "the process logs again using the returned cursor, then kill the process. "
        "Do not use bash backgrounding.",
        scripts={"fixture_server.py": (
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        print('REQUEST_HANDLED', flush=True)\n"
            "        self.send_response(200); self.end_headers(); self.wfile.write(b'HTTP_OK')\n"
            "    def log_message(self, *args): pass\n"
            "print('SERVER_READY', flush=True)\n"
            "HTTPServer(('127.0.0.1', 8765), H).serve_forever()\n"
        )},
        required_operations={"start": 1, "read": 2, "kill": 1},
        expected_output_terms=("SERVER_READY", "REQUEST_HANDLED", "HTTP_OK"),
        require_cursor_reuse=True,
    )


def _fixture_p2(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P2",
        "Use the persistent process tool to start `python -u watcher.py`. Read "
        "WATCH_READY, update watched.txt from alpha to beta with a file editing "
        "tool, then read only new watcher output using next_cursor and observe "
        "CHANGED:beta. Kill the watcher when finished.",
        scripts={
            "watched.txt": "alpha\n",
            "watcher.py": (
                "from pathlib import Path\nimport time\n"
                "path = Path('watched.txt'); previous = path.read_text()\n"
                "print('WATCH_READY', flush=True)\n"
                "while True:\n"
                "    current = path.read_text()\n"
                "    if current != previous:\n"
                "        previous = current\n"
                "        print('CHANGED:' + current.strip(), flush=True)\n"
                "    time.sleep(0.05)\n"
            ),
        },
        required_operations={"start": 1, "read": 2, "kill": 1},
        expected_output_terms=("WATCH_READY", "CHANGED:beta"),
        require_cursor_reuse=True,
    )


def _fixture_p3(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P3",
        "Start `python -u fixture_repl.py` with the persistent process tool. "
        "Read REPL_READY, write `1 + 1` followed by a newline and read result 2. "
        "Then write `6 * 7` and read result 42 in a later interaction. Reuse each "
        "next_cursor and kill the REPL after both results.",
        scripts={"fixture_repl.py": (
            "import sys\nprint('REPL_READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('RESULT:' + str(eval(line.strip(), {'__builtins__': {}})), flush=True)\n"
        )},
        required_operations={"start": 1, "read": 3, "write": 2, "kill": 1},
        expected_output_terms=("REPL_READY", "RESULT:2", "RESULT:42"),
        require_cursor_reuse=True,
    )


def _fixture_p4(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P4",
        "Start `python -u log_generator.py` as a persistent process. Read until "
        "LOG_1 appears, remember next_cursor, then read again from that cursor with "
        "a wait budget until a later log such as LOG_3 appears. The second result "
        "must not replay LOG_1. Kill the process afterward.",
        scripts={"log_generator.py": (
            "import time\n"
            "for i in range(1, 100):\n"
            "    print(f'LOG_{i}', flush=True)\n"
            "    time.sleep(0.2)\n"
        )},
        required_operations={"start": 1, "read": 2, "kill": 1},
        expected_output_terms=("LOG_1", "LOG_3"),
        require_cursor_reuse=True,
    )


def _fixture_p5(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P5",
        "Start `python -u crashing_service.py` with the persistent process tool. "
        "Read its output and inspect status after it crashes. Report CRASHING and "
        "the exact exit code 7; do not claim it is still running. No process should "
        "remain afterward.",
        scripts={"crashing_service.py": (
            "import sys, time\nprint('CRASHING', flush=True)\ntime.sleep(0.1)\nsys.exit(7)\n"
        )},
        required_operations={"start": 1, "read": 1, "status": 1},
        expected_output_terms=("CRASHING", '"exit_code": 7'),
        expected_exit_code=7,
    )


def _fixture_p6(root: Path) -> BehaviorTask:
    return _process_task(
        root,
        "P6",
        "Start two simultaneous persistent processes: `python -u service.py A` "
        "and `python -u service.py B`. Keep their process IDs separate, read "
        "SERVICE_A only from the first and SERVICE_B only from the second, inspect "
        "their status, then kill both. Leave zero processes running.",
        scripts={"service.py": (
            "import sys, time\nprint('SERVICE_' + sys.argv[1], flush=True)\ntime.sleep(60)\n"
        )},
        required_operations={"start": 2, "read": 2, "status": 1, "kill": 2},
        expected_output_terms=("SERVICE_A", "SERVICE_B"),
        min_processes=2,
    )


_FIXTURES = {
    "A": _fixture_a, "B": _fixture_b, "C": _fixture_c, "D": _fixture_d,
    "E": _fixture_e, "F": _fixture_f, "G": _fixture_g, "H": _fixture_h,
    "I": _fixture_i, "I2": _fixture_i2, "I3": _fixture_i3,
    "I4": _fixture_i4, "IS": _fixture_is,
    "V1": _fixture_v1, "V2": _fixture_v2, "V3": _fixture_v3,
    "V4": _fixture_v4, "V5": _fixture_v5, "V6": _fixture_v6,
    "V7": _fixture_v7, "V8": _fixture_v8,
    "C1": _fixture_c1, "C2": _fixture_c2, "C3": _fixture_c3, "C4": _fixture_c4,
    "W1": _fixture_w1, "W2": _fixture_w2, "W3": _fixture_w3,
    "W4": _fixture_w4, "W5": _fixture_w5,
    "P1": _fixture_p1, "P2": _fixture_p2, "P3": _fixture_p3,
    "P4": _fixture_p4, "P5": _fixture_p5, "P6": _fixture_p6,
}


def _is_benchmark_source_path(path: str) -> bool:
    value = Path(path)
    return (
        not any(part in {".git", ".agent", ".nz-coder", ".pytest_cache", "__pycache__"}
                for part in value.parts)
        and value.suffix not in {".pyc", ".pyo"}
    )


def _file_hashes(workspace: Path) -> dict[str, str]:
    result = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if not _is_benchmark_source_path(relative):
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _file_contents(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if _is_benchmark_source_path(relative):
            result[relative] = path.read_text(encoding="utf-8", errors="replace")
    return result


def _render_patch(before: dict[str, str], after: dict[str, str]) -> str:
    sections: list[str] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        sections.extend(difflib.unified_diff(
            before.get(path, "").splitlines(keepends=True),
            after.get(path, "").splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))
    return "".join(sections)


def _initialize_fixture_repository(workspace: Path) -> None:
    """Give every real-model fixture an isolated diff and status boundary."""
    commands = (
        ("git", "init", "-q"),
        ("git", "add", "."),
        (
            "git", "-c", "user.name=NZ-Coder Benchmark", "-c",
            "user.email=benchmark@example.invalid", "commit", "-qm", "fixture baseline",
        ),
    )
    for command in commands:
        completed = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"fixture git initialization failed: {' '.join(command)}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )


def _failure_category(score: dict) -> str | None:
    if score.get("success"):
        return None
    error = str(score.get("error") or "").casefold()
    if error:
        if "stopped_by_hook" in error:
            return "verification policy failure"
        return "provider/runtime failure"
    if score.get("capability") in {
        "unknown-location-localization", "process-understanding",
        "large-repo-navigation", "tool-scale",
    } and (not score.get("correct_files") or score.get("call_path_correct") is False):
        return "localization failure"
    diagnostics = score.get("trajectory_diagnostics") or {}
    if diagnostics.get("tool_selection_errors"):
        return "tool selection failure"
    if score.get("final_patch_correctness") is False:
        return "edit failure"
    verification = score.get("verification") or {}
    if verification.get("passed") is False:
        return "verification failure"
    if not score.get("recovery_complete", True):
        return "recovery failure"
    return "reasoning/context failure"


def _ordered_path(response: str, path: tuple[str, ...]) -> bool:
    cursor = 0
    lowered = response.casefold()
    for item in path:
        position = lowered.find(item.casefold(), cursor)
        if position < 0:
            return False
        cursor = position + len(item)
    return True


def _retrieval_metrics(events: tuple[dict, ...], task: BehaviorTask) -> dict:
    """Derive localization timing and precision from the actual tool trajectory."""
    expected_files = {path.casefold() for path in task.expected_files}
    expected_symbols = {symbol.casefold() for symbol in task.expected_symbols}
    turn = 0
    first_ts = next((
        float(event["ts"]) for event in events
        if isinstance(event.get("ts"), (int, float))
    ), None)
    localization_turn = None
    localization_ms = None
    file_reads = correct_reads = 0
    ri_candidates = correct_candidates = 0
    for event in events:
        kind = str(event.get("event") or event.get("type") or "")
        if kind in {"model_call", "llm_response"}:
            turn += 1
            continue
        if kind not in {"tool_result", "tool_call"}:
            continue
        name = str(event.get("tool_name") or event.get("name") or "")
        raw_input = event.get("input")
        raw_input = raw_input if isinstance(raw_input, dict) else {}
        path = str(
            event.get("path") or raw_input.get("path") or raw_input.get("file_path") or ""
        ).replace("\\", "/").lstrip("./")
        output = str(event.get("output") or "")
        hit = False
        if name in {"read", "read_file"}:
            file_reads += 1
            if path.casefold() in expected_files:
                correct_reads += 1
                hit = True
        if name in {"repo_context", "semantic_search", "repo_map", "code_references"}:
            try:
                payload = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                payload = {}
            candidates = payload.get("items") or payload.get("matches") or ()
            for item in candidates if isinstance(candidates, list) else ():
                if not isinstance(item, dict):
                    continue
                ri_candidates += 1
                candidate_text = " ".join(str(value) for value in (
                    item.get("file"), item.get("path"), item.get("locator"),
                    item.get("symbol_id"), item.get("identity"), item.get("title"),
                ) if value).casefold()
                correct = any(path in candidate_text for path in expected_files)
                correct = correct or any(symbol in candidate_text for symbol in expected_symbols)
                if correct:
                    correct_candidates += 1
                    hit = True
            if not hit:
                lowered = output.casefold()
                hit = any(path in lowered for path in expected_files) or any(
                    symbol in lowered for symbol in expected_symbols
                )
        if hit and localization_turn is None:
            localization_turn = max(1, turn)
            timestamp = event.get("ts")
            if first_ts is not None and isinstance(timestamp, (int, float)):
                localization_ms = round(max(0.0, (float(timestamp) - first_ts) * 1000), 3)
    return {
        "localization_turn": localization_turn,
        "time_to_first_correct_file_ms": localization_ms,
        "retrieval_precision": round(correct_reads / max(1, file_reads), 4),
        "ri_candidate_precision": round(correct_candidates / max(1, ri_candidates), 4),
    }


def _verification_reliability_metrics(events: tuple[dict, ...]) -> dict:
    from nz_coder.intelligence.verification_planner import classify_verification_command

    has_execution_events = any(
        str(event.get("event") or event.get("type") or "") == "tool_call"
        and str(event.get("name") or event.get("tool_name") or "")
        in {"bash", "verify_changed_files"}
        for event in events
    )
    turn = 0
    first_failure_turn = None
    recovery_turn = None
    failures = recoveries = targeted = regression = 0
    failed_commands: dict[str, int] = {}
    active_failure = False
    for event in events:
        kind = str(event.get("event") or event.get("type") or "")
        if kind in {"model_call", "llm_response"}:
            turn += 1
            continue
        # Production traces emit a tool_call and then lifecycle events for the
        # same command. Controlled CI traces intentionally contain only the
        # latter, so prefer executions and fall back only when none exist.
        if has_execution_events and kind != "tool_call":
            continue
        if not has_execution_events and kind not in {"verification", "verification_result"}:
            continue
        name = str(event.get("name") or event.get("tool_name") or "")
        raw_input = event.get("input")
        command = str(
            event.get("command")
            or (raw_input.get("command") if isinstance(raw_input, dict) else "")
            or ("verify_changed_files" if name == "verify_changed_files" else "")
        ).strip()
        if not command:
            continue
        if has_execution_events:
            if name != "bash" and name != "verify_changed_files":
                continue
            failed = bool(
                event.get("command_failed")
                or event.get("dispatch_failed")
                or event.get("status") in {"error", "nonzero"}
            )
            output = str(event.get("output") or "")
            if name == "verify_changed_files":
                failed = output.startswith(("FAIL:", "Error:"))
        else:
            failed = not bool(event.get("success") or event.get("passed"))
        stage = classify_verification_command(command)
        targeted += int(stage == "targeted")
        regression += int(stage == "regression")
        if failed:
            failures += 1
            active_failure = True
            failed_commands[command] = failed_commands.get(command, 0) + 1
            if first_failure_turn is None:
                first_failure_turn = max(1, turn)
        elif active_failure:
            recoveries += 1
            active_failure = False
            if recovery_turn is None:
                recovery_turn = max(1, turn)
    return {
        "verification_failures": failures,
        "verification_recoveries": recoveries,
        "time_to_first_failure_turn": first_failure_turn,
        "turn_to_recovery": recovery_turn,
        "same_failed_command_count": max(failed_commands.values(), default=0),
        "targeted_test_count": targeted,
        "regression_test_count": regression,
    }


def _process_contract(task: BehaviorTask, observation: BehaviorObservation) -> dict | None:
    if task.capability != "persistent-process":
        return None
    calls = [
        event for event in observation.events
        if str(event.get("event") or event.get("type") or "") in {"tool_call", "tool_result"}
        and str(event.get("name") or event.get("tool_name") or "") == "process"
    ]
    counts: dict[str, int] = {}
    process_ids: set[str] = set()
    returned_process_ids: set[str] = set()
    read_history: dict[str, list[tuple[int | None, int | None, str]]] = {}
    output_parts = [observation.final_response]
    exit_codes: set[int] = set()
    for event in calls:
        raw_input = event.get("input")
        arguments = raw_input if isinstance(raw_input, dict) else {}
        operation = str(arguments.get("operation") or "").strip().lower()
        if operation == "list":
            operation = "status"
        counts[operation] = counts.get(operation, 0) + 1
        argument_id = str(arguments.get("process_id") or "").strip()
        # ``start`` accepts no caller-selected identity. Some models still
        # send a descriptive process_id, which the tool correctly ignores;
        # counting it would create a false multi-process result.
        output = str(event.get("output") or "")
        output_parts.append(output)
        try:
            payload = json.loads(output)
        except (TypeError, ValueError):
            payload = {}
        process = payload.get("process") if isinstance(payload, dict) else None
        if isinstance(process, dict):
            process_id = str(process.get("process_id") or "").strip()
            if process_id:
                process_ids.add(process_id)
                if operation == "start":
                    returned_process_ids.add(process_id)
            exit_code = process.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                exit_codes.add(exit_code)
        if operation == "read":
            process_id = argument_id or str(payload.get("process_id") or "")
            supplied_cursor = arguments.get("cursor")
            next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
            if isinstance(payload, dict):
                exit_code = payload.get("exit_code")
                if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                    exit_codes.add(exit_code)
            read_history.setdefault(process_id, []).append((
                supplied_cursor if isinstance(supplied_cursor, int) else None,
                next_cursor if isinstance(next_cursor, int) else None,
                output,
            ))

    # Only identities issued by successful starts are real benchmark
    # processes. Recovered attempts using a caller-invented alias remain in
    # wrong_process_access metrics but cannot inflate multi-process coverage.
    process_ids.intersection_update(returned_process_ids)

    required = {
        str(name): int(count)
        for name, count in dict(
            task.metadata.get("required_process_operations") or {}
        ).items()
    }
    operations_complete = all(counts.get(name, 0) >= count for name, count in required.items())
    combined = "\n".join(output_parts)
    expected_terms = tuple(
        str(item) for item in task.metadata.get("expected_process_output_terms", ())
    )
    output_complete = all(term.casefold() in combined.casefold() for term in expected_terms)
    distinct_complete = len(process_ids) >= int(task.metadata.get("min_distinct_processes") or 1)
    cursor_reused = True
    if task.metadata.get("require_cursor_reuse"):
        cursor_reused = False
        for history in read_history.values():
            for previous, current in zip(history, history[1:]):
                if previous[1] is not None and current[0] == previous[1]:
                    cursor_reused = True
                    break
            if cursor_reused:
                break
    expected_exit = task.metadata.get("expected_process_exit_code")
    exit_complete = (
        expected_exit is None
        or int(expected_exit) in exit_codes
        or f"exit code {int(expected_exit)}" in combined.casefold()
        or f"exit_code={int(expected_exit)}" in combined.casefold()
    )
    benchmark_event = next(
        (
            event for event in reversed(observation.events)
            if str(event.get("event") or event.get("type") or "") == "process_benchmark"
        ),
        {},
    )
    orphan_count = int(benchmark_event.get("orphan_process_count") or 0)
    zero_orphans = not task.metadata.get("require_zero_orphans") or orphan_count == 0
    wrong_access = sum(
        "unknown process_id" in str(event.get("output") or "").casefold()
        or "belongs to another session" in str(event.get("output") or "").casefold()
        for event in calls
    )
    success = bool(
        operations_complete and output_complete and distinct_complete
        and cursor_reused and exit_complete and zero_orphans
    )
    return {
        "success": success,
        "operation_counts": counts,
        "required_operations": required,
        "operations_complete": operations_complete,
        "expected_output_complete": output_complete,
        "distinct_processes": len(process_ids),
        "distinct_processes_complete": distinct_complete,
        "cursor_reused": cursor_reused,
        "exit_code_observed": sorted(exit_codes),
        "exit_code_complete": exit_complete,
        "orphan_process_count": orphan_count,
        "zero_orphans": zero_orphans,
        "wrong_process_access": wrong_access,
    }


def _score(
    task: BehaviorTask, workspace: Path, before: dict[str, str],
    observation: BehaviorObservation, elapsed_ms: float,
    config: BehaviorBenchmarkConfig,
) -> dict:
    response = observation.final_response
    after = _file_hashes(workspace)
    changed = sorted(path for path in ({
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    } | set(observation.changed_files)) if _is_benchmark_source_path(path))
    correct_files = [path for path in task.expected_files if path.casefold() in response.casefold()]
    correct_symbols = [name for name in task.expected_symbols if name.casefold() in response.casefold()]
    read_paths = [
        str(event.get("path") or "") for event in observation.events
        if str(event.get("event") or event.get("type") or "") in {"tool_result", "tool_call"}
        and str(event.get("tool_name") or event.get("name") or "") in {"read", "read_file"}
    ]
    for index, path in enumerate(read_paths):
        if path:
            continue
        event = [
            value for value in observation.events
            if str(value.get("event") or value.get("type") or "") in {"tool_result", "tool_call"}
            and str(value.get("tool_name") or value.get("name") or "") in {"read", "read_file"}
        ][index]
        raw_input = event.get("input")
        if isinstance(raw_input, dict):
            read_paths[index] = str(raw_input.get("path") or raw_input.get("file_path") or "")
    wrong_reads = [
        path for path in read_paths if path and path not in task.expected_files
        and not path.startswith(("tests/", ".nz-coder/"))
    ]
    verification = {"attempted": False, "passed": None, "returncode": None}
    if task.verification_command:
        verification["attempted"] = True
        try:
            completed = subprocess.run(
                list(task.verification_command), cwd=workspace,
                capture_output=True, text=True, timeout=120, check=False,
            )
            verification.update({
                "passed": completed.returncode == 0,
                "returncode": completed.returncode,
                "output_tail": (completed.stdout + completed.stderr)[-2000:],
            })
        except (OSError, subprocess.SubprocessError) as exc:
            verification.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
    patch_checks = []
    required = str(task.metadata.get("required_text") or "")
    forbidden = str(task.metadata.get("forbidden_text") or "")
    if required:
        required_paths = task.metadata.get("required_in_files") or task.metadata.get("must_change")
        required_paths = required_paths or [path for path in task.expected_files if path in changed]
        patch_checks.append(bool(required_paths) and all(
            required in (workspace / path).read_text(encoding="utf-8", errors="replace")
            for path in required_paths if (workspace / path).is_file()
        ))
    if forbidden:
        patch_checks.append(all(
            forbidden not in path.read_text(encoding="utf-8", errors="replace")
            for path in workspace.rglob("*")
            if path.is_file() and ".nz-coder" not in path.parts and ".agent" not in path.parts
        ))
    must_change = [str(item) for item in task.metadata.get("must_change", ())]
    if must_change:
        patch_checks.append(all(path in changed for path in must_change))
    metrics = AgentTrajectoryMetrics.from_events(list(observation.events))
    metrics_payload = asdict(metrics)
    metrics_payload.update(_retrieval_metrics(observation.events, task))
    metrics_payload.update(_verification_reliability_metrics(observation.events))
    metrics_payload["wall_time_ms"] = elapsed_ms
    evidence_file = str(task.metadata.get("evidence_file") or "")
    external_evidence_correct: bool | None = None
    if evidence_file:
        evidence_path = workspace / evidence_file
        evidence_text = (
            evidence_path.read_text(encoding="utf-8", errors="replace")
            if evidence_path.is_file() else ""
        )
        required_terms = tuple(str(item) for item in task.metadata.get("required_terms", ()))
        source_domains = tuple(str(item) for item in task.metadata.get("source_domains", ()))
        external_evidence_correct = (
            bool(evidence_text.strip())
            and all(term.casefold() in evidence_text.casefold() for term in required_terms)
            and any(domain.casefold() in evidence_text.casefold() for domain in source_domains)
        )
        patch_checks.append(external_evidence_correct)
    elif task.metadata.get("response_evidence"):
        required_terms = tuple(str(item) for item in task.metadata.get("required_terms", ()))
        source_domains = tuple(str(item) for item in task.metadata.get("source_domains", ()))
        external_evidence_correct = (
            bool(response.strip())
            and all(term.casefold() in response.casefold() for term in required_terms)
            and any(domain.casefold() in response.casefold() for domain in source_domains)
        )
        patch_checks.append(external_evidence_correct)
    no_unneeded_web = (
        not task.metadata.get("forbid_web_search")
        or int(metrics_payload.get("web_search_calls") or 0) == 0
    )
    process_contract = _process_contract(task, observation)
    localization_complete = (
        len(correct_files) == len(task.expected_files)
        and len(correct_symbols) == len(task.expected_symbols)
        and (not task.expected_call_path or _ordered_path(response, task.expected_call_path))
    )
    patch_correct = all(patch_checks) if patch_checks else None
    recovery_required = bool(task.metadata.get("requires_verification_recovery"))
    reference_trajectory_unavailable = (
        str(observation.run_result.get("reference") or "") != ""
        and observation.run_result.get("reference_trajectory_available") is False
    )
    long_horizon_exercised: bool | None = (
        None
        if reference_trajectory_unavailable
        else metrics.turns >= task.min_turns
    )
    recovery_complete: bool | None = (
        None if recovery_required and reference_trajectory_unavailable
        else (
            not recovery_required
            or (metrics.failed_commands >= 1 and metrics.verification_recoveries >= 1)
        )
    )
    child_execution_complete = (
        task.capability != "multi-agent"
        or metrics.child_sessions >= max(0, int(config.child_agents) - 1)
    )
    raw_status = str((observation.run_result.get("metadata") or {}).get("raw_status") or "")
    expected_degraded = bool(task.metadata.get("expected_degraded"))
    degraded_correct = not expected_degraded or raw_status == "completed_unverified"
    success = (
        not observation.error
        and (localization_complete if task.capability in {
            "unknown-location-localization", "process-understanding",
            "large-repo-navigation", "tool-scale",
        } else True)
        and (verification["passed"] is not False)
        and (patch_correct is not False)
        and recovery_complete is not False
        and child_execution_complete
        and degraded_correct
        and no_unneeded_web
        and (process_contract is None or process_contract["success"])
    )
    runtime_completed = not observation.error and raw_status in {"completed", "completed_unverified", ""}
    false_pass = bool(
        runtime_completed
        and (verification.get("passed") is False or patch_correct is False)
    )
    false_block = bool(
        observation.error
        and verification.get("passed") is True
        and patch_correct is not False
    )
    return {
        "case_id": task.case_id, "capability": task.capability, "success": success,
        "final_patch_correctness": patch_correct,
        "correct_files": correct_files, "correct_symbols": correct_symbols,
        "call_path_correct": _ordered_path(response, task.expected_call_path)
        if task.expected_call_path else None,
        "wrong_file_reads": wrong_reads, "changed_files": changed,
        "verification": verification, "metrics": metrics_payload,
        "trajectory_diagnostics": asdict(diagnose_trajectory(list(observation.events))),
        "recovery_complete": recovery_complete,
        "turn_requirement_observable": not reference_trajectory_unavailable,
        "long_horizon_exercised": long_horizon_exercised,
        "runtime_status": raw_status or str(observation.run_result.get("status") or ""),
        "verification_state": (
            "degraded" if raw_status == "completed_unverified"
            else ("passed" if verification.get("passed") is True else "failed")
        ),
        "false_pass": false_pass,
        "false_block": false_block,
        "external_evidence_correct": external_evidence_correct,
        "no_unneeded_web": no_unneeded_web,
        "child_execution_complete": child_execution_complete,
        "process_contract": process_contract,
        "run_result": observation.run_result, "error": observation.error,
    }


class AgentBehaviorBenchmark:
    """Run real task fixtures through an Agent-owned execution driver."""

    def __init__(self, output_dir: Path, driver: BehaviorDriver) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.driver = driver

    def run_case(self, case_id: str, config: BehaviorBenchmarkConfig) -> dict:
        if case_id not in _FIXTURES:
            raise KeyError(f"Unknown behavior benchmark case: {case_id}")
        run_id = (
            f"{case_id}-{config.repo_intelligence}-r{config.repetition}-{time.time_ns()}"
        )
        workspace = self.output_dir / "workspaces" / run_id
        workspace.mkdir(parents=True, exist_ok=False)
        task = _FIXTURES[case_id](workspace)
        _initialize_fixture_repository(workspace)
        before = _file_hashes(workspace)
        before_contents = _file_contents(workspace)
        started = time.perf_counter()
        try:
            observation = self.driver.run(task, workspace, config)
        except Exception as exc:
            observation = BehaviorObservation(
                "", error=f"{type(exc).__name__}: {exc}",
            )
        if task.capability == "persistent-process":
            from nz_coder.runtime.process.process_service import (
                close_workspace_process_service,
                workspace_process_service,
            )

            service = workspace_process_service(workspace)
            active = service.list(active_only=True)
            process_event = {
                "event": "process_benchmark",
                "orphan_process_count": len(active),
                "active_process_ids": [item.process_id for item in active],
            }
            observation = BehaviorObservation(
                final_response=observation.final_response,
                events=tuple(observation.events) + (process_event,),
                run_result=observation.run_result,
                changed_files=observation.changed_files,
                error=observation.error,
            )
            # The harness never performs task interaction, but it always seals
            # fixture isolation after measuring the Agent's cleanup behavior.
            close_workspace_process_service(workspace)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        score = _score(task, workspace, before, observation, elapsed_ms, config)
        after_contents = _file_contents(workspace)
        visited = sorted({
            str(event.get("path") or (event.get("input") or {}).get("path") or "")
            for event in observation.events
            if isinstance(event.get("input") or {}, dict)
            and str(event.get("path") or (event.get("input") or {}).get("path") or "")
        })
        evidence_kind = str(getattr(self.driver, "evidence_kind", "unknown"))
        result = {
            "benchmark_version": 3,
            "suite_type": f"agent-behavior-{evidence_kind}",
            "evidence_kind": evidence_kind,
            "task": asdict(task), "config": asdict(config), "score": score,
            "final_response": observation.final_response,
            "trace": {
                "events": list(observation.events), "files_visited": visited,
                "messages": [
                    event for event in observation.events
                    if str(event.get("event") or "") in {
                        "llm_request", "llm_response", "assistant_message",
                    }
                ],
                "verification": score["verification"],
                "final_patch": _render_patch(before_contents, after_contents),
                "error": observation.error,
                "failure_category": _failure_category(score),
            },
        }
        report_dir = self.output_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{run_id}.json").write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8",
        )
        return result

    def run_matrix(
        self, case_ids: tuple[str, ...], configs: tuple[BehaviorBenchmarkConfig, ...],
    ) -> dict:
        runs = [self.run_case(case_id, config) for config in configs for case_id in case_ids]
        return {
            "benchmark_version": 3,
            "suite_type": f"agent-behavior-{getattr(self.driver, 'evidence_kind', 'unknown')}",
            "evidence_kind": str(getattr(self.driver, "evidence_kind", "unknown")),
            "runs": runs,
            "success_rate": sum(bool(run["score"]["success"]) for run in runs) / max(1, len(runs)),
        }


class _LocalizationTools:
    def __init__(
        self, workspace: Path, events: list[dict],
        config: BehaviorBenchmarkConfig | None = None,
    ) -> None:
        self.workspace = workspace
        self.events = events
        self.config = config or BehaviorBenchmarkConfig(model="controlled")
        self.service = RepoIntelligenceService(workspace)

    def close(self) -> None:
        self.service.close()

    def _repo_context(self, arguments: dict) -> str:
        if self.service.state.status == "cold":
            self.service.prewarm(max_files=100).result(timeout=10)
        operation = str(arguments.get("operation") or "")
        query = str(arguments.get("module") or "")
        if operation == "symbol_search":
            result = self.service.search_symbols(query, limit=20)
        elif operation == "lookup":
            if self.config.repo_intelligence != "lookup":
                result = {"error": "lookup disabled for this benchmark tier"}
            else:
                result = self.service.intent_lookup(query, limit=20)
        elif operation == "symbol_context":
            result = self.service.symbol_context(query, limit=20)
        elif operation == "process_context":
            result = self.service.process_context(query, max_depth=8, limit=50)
        else:
            result = {"error": f"unsupported operation {operation}"}
        return json.dumps(result, sort_keys=True)

    def _grep(self, arguments: dict) -> str:
        query = str(arguments.get("query") or "")
        rows = []
        for path in sorted(self.workspace.rglob("*.py")):
            if ".nz-coder" in path.parts:
                continue
            for line, value in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if query.casefold() in value.casefold():
                    rows.append(f"{path.relative_to(self.workspace).as_posix()}:{line}:{value}")
        return "\n".join(rows)

    def _read(self, arguments: dict) -> str:
        path = str(arguments.get("path") or "")
        target = (self.workspace / path).resolve()
        target.relative_to(self.workspace)
        return target.read_text(encoding="utf-8", errors="replace")

    def _write_file(self, arguments: dict) -> str:
        path = str(arguments.get("path") or "")
        target = (self.workspace / path).resolve()
        target.relative_to(self.workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("content") or ""), encoding="utf-8")
        return f"Wrote {path}"

    def _bash(self, arguments: dict) -> tuple[str, bool]:
        command = str(arguments.get("command") or "")
        completed = subprocess.run(
            command, cwd=self.workspace, shell=True, capture_output=True,
            text=True, timeout=60, check=False,
        )
        output = (completed.stdout + completed.stderr)[-4000:]
        return output or f"exit code {completed.returncode}", completed.returncode != 0

    def _child_task(self, arguments: dict) -> str:
        services = tuple(
            item for item in str(arguments.get("services") or "").split(",") if item
        )
        child_events: list[dict] = []
        child_tools = _LocalizationTools(self.workspace, child_events, self.config)
        try:
            run = run_native_agent_scenario(
                self.workspace,
                prompt=f"Update status constant and tests for: {', '.join(services)}",
                model=_ControlledServiceChildModel(services, child_events),
                tools=child_tools, max_turns=6,
                tool_names=("read_file", "write_file"),
                session_id=f"behavior-child-{time.time_ns()}",
            )
        finally:
            child_tools.close()
        self.events.extend(child_events)
        self.events.append({
            "event": "child_session", "services": list(services), "conflicts": 0,
        })
        return str(run["result"].get("content") or "child completed")

    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        task_results: dict[str, str] = {}
        task_calls = []
        for call in calls:
            function = call.get("function") or {}
            if str(function.get("name") or "") != "task":
                continue
            raw = function.get("arguments") or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            task_calls.append((str(call["id"]), arguments))
        if task_calls:
            outputs = await asyncio.gather(*(
                asyncio.to_thread(self._child_task, arguments)
                for _call_id, arguments in task_calls
            ))
            task_results = {
                call_id: output for (call_id, _arguments), output in zip(task_calls, outputs)
            }
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw = function.get("arguments") or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            failed = False
            if name == "repo_context":
                output = self._repo_context(arguments)
            elif name == "grep_search":
                output = self._grep(arguments)
            elif name == "read_file":
                output = self._read(arguments)
            elif name == "write_file":
                output = self._write_file(arguments)
            elif name == "bash":
                output, failed = self._bash(arguments)
            elif name == "tool_search":
                output = json.dumps({"matches": ["grep_search", "read_file"]})
            elif name == "task":
                output = task_results[str(call["id"])]
            else:
                output = f"unsupported tool: {name}"
            call_id = str(call["id"])
            processor.complete_tool(call_id, output)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            event = {
                "event": "tool_result", "tool_name": name,
                "tokens": max(1, len(output) // 4), "output": output,
            }
            if name == "read_file":
                event["path"] = str(arguments.get("path") or "")
            if name == "write_file":
                event["path"] = str(arguments.get("path") or "")
            if name == "grep_search":
                event["query"] = str(arguments.get("query") or "")
            if name == "repo_context":
                event["query"] = str(arguments.get("module") or "")
                event["localized"] = arguments.get("operation") in {
                    "lookup", "symbol_context", "process_context",
                }
            if name == "bash":
                event["command"] = str(arguments.get("command") or "")
                event["failed"] = failed
                self.events.append({
                    "event": "verification", "command": event["command"],
                    "success": not failed,
                })
            self.events.append(event)
            failed = False
        processor.finish_step("tool-calls")
        return "continue"


class _ControlledServiceChildModel:
    def __init__(self, services: tuple[str, ...], events: list[dict]) -> None:
        self.services = services
        self.events = events
        self.stage = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.events.append({
            "event": "model_call", "input_tokens": 80, "output_tokens": 20,
            "schema_tokens": 100, "context_window": 20_000,
        })
        if self.stage == 0:
            calls = [
                _ControlledBehaviorModel.call(f"read-{service}-{suffix}", "read_file", {"path": path})
                for service in self.services
                for suffix, path in (
                    ("api", f"{service}/api.py"), ("test", f"tests/test_{service}.py"),
                )
            ]
        elif self.stage == 1:
            calls = []
            for service in self.services:
                calls.extend((
                    _ControlledBehaviorModel.call(
                        f"write-{service}-api", "write_file", {
                            "path": f"{service}/api.py",
                            "content": (
                                "STATUS = 'ok'\n"
                                f"def {service}_health(): return STATUS\n"
                            ),
                        },
                    ),
                    _ControlledBehaviorModel.call(
                        f"write-{service}-test", "write_file", {
                            "path": f"tests/test_{service}.py",
                            "content": (
                                f"from {service}.api import STATUS, {service}_health\n"
                                f"def test_health(): assert {service}_health() == STATUS == 'ok'\n"
                            ),
                        },
                    ),
                ))
        else:
            return LLMResult(
                content=f"Updated services: {', '.join(self.services)}",
                finish_reason="stop", input_tokens=80, output_tokens=20,
            )
        self.stage += 1
        return LLMResult(
            content="", tool_calls=calls, finish_reason="tool_calls",
            input_tokens=80, output_tokens=20,
        )


class _ControlledBehaviorModel:
    """Deterministic task policy whose only effects are emitted tool calls."""

    def __init__(
        self, task: BehaviorTask, config: BehaviorBenchmarkConfig, events: list[dict],
    ) -> None:
        self.task = task
        self.config = config
        self.events = events
        self.stage = 0

    @staticmethod
    def call(call_id: str, name: str, arguments: dict) -> dict:
        return {
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    @staticmethod
    def _rename_writes(include_edge: bool = False) -> list[dict]:
        files = {
            "catalog/api.py": "# public product renderer\ndef render_product(product):\n    return product['name']\n",
            "web/controller.py": (
                "# migrated public API\n"
                "from catalog.api import render_product\n"
                "def show(p): return render_product(p)\n"
            ),
            "cli/report.py": (
                "# migrated public API\n"
                "from catalog.api import render_product\n"
                "def report(p): return render_product(p)\n"
            ),
            "mobile/presenter.py": (
                "# migrated public API\n"
                "from catalog.api import render_product\n"
                "def title(p): return render_product(p)\n"
            ),
            "tests/test_catalog_api.py": (
                "# render_product contract\n"
                "from catalog.api import render_product\n"
                "def test_format(): assert render_product({'name':'A'}) == 'A'\n"
            ),
            "tests/test_report.py": (
                "# render_product caller\n"
                "from catalog.api import render_product\n"
                "def test_report(): assert render_product({'name':'A'}) == 'A'\n"
            ),
        }
        if include_edge:
            files["tests/test_edge_case.py"] = (
                "# render_product edge case\n"
                "from catalog.api import render_product\n"
                "def test_missing_name(): assert render_product({'name': ''}) == ''\n"
            )
        return [
            _ControlledBehaviorModel.call(f"write-{index}", "write_file", {
                "path": path, "content": content,
            })
            for index, (path, content) in enumerate(files.items())
        ]

    def _actions(self) -> list[dict] | None:
        case = self.task.case_id
        stage = self.stage
        if case == "A":
            if stage == 0:
                operation = "lookup" if self.config.repo_intelligence == "lookup" else "symbol_search"
                return [self.call("locate", "grep_search", {"query": "refresh expired"})] if self.config.repo_intelligence == "off" else [self.call("locate", "repo_context", {"operation": operation, "module": "refresh expired"})]
            if stage == 1:
                operation = "process_context" if self.config.repo_intelligence in {"v3", "lookup"} else "symbol_context"
                return [self.call("context", "repo_context", {"operation": operation, "module": "refresh_if_expired"})] if self.config.repo_intelligence != "off" else [self.call("read-token", "read_file", {"path": "auth/tokens.py"})]
            if stage == 2:
                paths = (
                    ("auth/tokens.py",) if self.config.repo_intelligence in {"v3", "lookup"}
                    else (*self.task.expected_files, "billing/tokens.py")
                )
                return [self.call(f"read-a-{index}", "read_file", {"path": path}) for index, path in enumerate(paths)]
            return None
        if case in {"B", "E"}:
            if case == "B":
                if stage == 0:
                    if self.config.repo_intelligence == "off":
                        return [self.call("impact", "grep_search", {"query": "format_product"})]
                    operation = (
                        "lookup" if self.config.repo_intelligence == "lookup"
                        else "symbol_context" if self.config.repo_intelligence == "v3"
                        else "symbol_search"
                    )
                    return [self.call("impact", "repo_context", {"operation": operation, "module": "format_product"})]
                if stage == 1:
                    paths = (
                        self.task.expected_files
                        if self.config.repo_intelligence not in {"v3", "lookup"}
                        else (
                            "catalog/api.py", "web/controller.py", "cli/report.py",
                            "mobile/presenter.py",
                        )
                    )
                    return [self.call(f"read-b-{index}", "read_file", {"path": path}) for index, path in enumerate(paths)]
                if stage == 2:
                    return self._rename_writes()
                if stage == 3:
                    return [self.call("verify-b", "bash", {"command": "python -m pytest -q"})]
                return None
            if stage == 0:
                return [self.call("impact-e", "repo_context", {"operation": "symbol_context", "module": "format_product"})]
            if 1 <= stage <= 8:
                return [self.call(f"read-e-{stage}", "read_file", {"path": self.task.expected_files[stage - 1]})]
            if stage == 9:
                return self._rename_writes()[:1]
            if stage == 10:
                return [self.call("verify-e-fail", "bash", {"command": "python -m pytest -q"})]
            if stage == 11:
                self.events.append({"event": "context_compaction", "input_tokens": 14_000, "context_window": 20_000})
                return self._rename_writes(include_edge=True)[1:]
            if stage == 12:
                return [self.call("verify-e-pass", "bash", {"command": "python -m pytest -q"})]
            if stage == 13:
                return [self.call("read-e-final", "read_file", {"path": "catalog/api.py"})]
            if stage == 14:
                return [self.call("context-e-final", "repo_context", {"operation": "symbol_context", "module": "render_product"})]
            return None
        if case == "C":
            if stage == 0:
                if self.config.repo_intelligence == "off":
                    return [self.call("process-c", "grep_search", {"query": "post user"})]
                operation = (
                    "lookup" if self.config.repo_intelligence == "lookup"
                    else "process_context" if self.config.repo_intelligence == "v3"
                    else "symbol_search"
                )
                return [self.call("process-c", "repo_context", {"operation": operation, "module": "post_user"})]
            if stage == 1:
                paths = self.task.expected_files if self.config.repo_intelligence not in {"v3", "lookup"} else ("http/routes.py", "storage/repository.py")
                return [self.call(f"read-c-{index}", "read_file", {"path": path}) for index, path in enumerate(paths)]
            return None
        if case == "D":
            if stage == 0:
                if self.config.repo_intelligence == "off":
                    return [self.call("search-d", "grep_search", {"query": "failed payment retry"})]
                operation = (
                    "lookup" if self.config.repo_intelligence == "lookup"
                    else "process_context" if self.config.repo_intelligence == "v3"
                    else "symbol_search"
                )
                return [self.call("search-d", "repo_context", {"operation": operation, "module": "retry_failed_payment"})]
            if stage == 1:
                paths = (
                    ("platform/retry/policy.py",)
                    if self.config.repo_intelligence in {"v3", "lookup"}
                    else self.task.expected_files
                )
                return [self.call(f"read-d-{index}", "read_file", {"path": path}) for index, path in enumerate(paths)]
            return None
        if case == "F":
            if stage == 0:
                return [self.call("verify-f-fail", "bash", {"command": "python -m pytest -q"})]
            if stage == 1:
                return [self.call(f"read-f-{index}", "read_file", {"path": path}) for index, path in enumerate(self.task.expected_files)]
            if stage == 2:
                return [self.call("write-f", "write_file", {
                    "path": "calc/service.py",
                    "content": "def ratio(total, count):\n    return total / count if count else 0\n",
                })]
            if stage == 3:
                return [self.call("verify-f-pass", "bash", {"command": "python -m pytest -q"})]
            return None
        if case == "G":
            services = ("users", "orders", "billing", "search")
            if stage == 0 and self.config.child_agents > 1:
                child_count = min(4, self.config.child_agents - 1)
                groups = [services[index::child_count] for index in range(child_count)]
                return [self.call(f"child-g-{index}", "task", {"services": ",".join(group)}) for index, group in enumerate(groups)]
            if stage == 0:
                calls = []
                for service in services:
                    calls.extend((
                        self.call(f"write-g-{service}-api", "write_file", {"path": f"{service}/api.py", "content": f"STATUS = 'ok'\ndef {service}_health(): return STATUS\n"}),
                        self.call(f"write-g-{service}-test", "write_file", {"path": f"tests/test_{service}.py", "content": f"from {service}.api import STATUS, {service}_health\ndef test_health(): assert {service}_health() == STATUS == 'ok'\n"}),
                    ))
                return calls
            if stage == 1:
                return [self.call("verify-g", "bash", {"command": "python -m pytest -q"})]
            return None
        if case == "H":
            if stage == 0 and self.config.progressive_exposure and self.config.tool_catalog_size >= 50:
                return [self.call("tool-search-h", "tool_search", {"query": "read email normalization source"})]
            if stage == 0:
                return [self.call("grep-h", "grep_search", {"query": "normalize email"})]
            if stage == 1:
                return [self.call("read-h", "read_file", {"path": "app.py"})]
            return None
        if case == "I":
            if stage == 0:
                if self.config.repo_intelligence == "off":
                    return [self.call("search-i", "grep_search", {"query": "cart record receipt"})]
                operation = "lookup" if self.config.repo_intelligence == "lookup" else "symbol_search"
                return [self.call("search-i", "repo_context", {
                    "operation": operation, "module": "cart record receipt",
                })]
            if stage == 1:
                return [
                    self.call(f"read-i-{index}", "read_file", {"path": path})
                    for index, path in enumerate(self.task.expected_files)
                ]
            return None
        return None

    def _final(self) -> str:
        return {
            "A": "app/routes/session.py session_route -> auth/tokens.py refresh_if_expired -> auth/client.py rotate_token. Modify those three locations and tests/test_token_refresh.py.",
            "B": "Renamed catalog/api.py format_product to render_product; updated web/controller.py, cli/report.py, mobile/presenter.py, tests/test_catalog_api.py, tests/test_report.py, and verified jobs/export.py remains an indirect caller.",
            "C": "http/routes.py post_user -> app/controller.py create_user -> domain/service.py register_user -> storage/repository.py save_user -> storage/client.py insert.",
            "D": "platform/retry/worker.py process calls platform/retry/policy.py retry_failed_payment, which permits attempts below three.",
            "E": "Completed render_product migration across catalog/api.py, web/controller.py, cli/report.py, mobile/presenter.py, tests/test_catalog_api.py, tests/test_report.py, tests/test_edge_case.py; jobs/export.py remains valid and verification recovered.",
            "F": "Fixed calc/service.py ratio for count zero after observing a failing tests/test_ratio.py run, then verified the repair.",
            "G": "Added STATUS to users/api.py, orders/api.py, billing/api.py, search/api.py and updated tests/test_users.py, tests/test_orders.py, tests/test_billing.py, tests/test_search.py.",
            "H": "app.py normalize_email strips surrounding whitespace and lowercases the email.",
            "I": (
                "gateway/checkout.py finalize_cart -> workflow/coordinator.py close_cart "
                "-> archive/store.py commit_record -> messaging/outbound.py dispatch_receipt."
            ),
        }[self.task.case_id]

    async def complete_turn(self, _context, _messages, **_kwargs):
        visible = (
            3 if self.config.progressive_exposure and self.config.tool_catalog_size >= 50
            else self.config.tool_catalog_size
        )
        self.events.append({
            "event": "model_call", "input_tokens": 120 + self.stage * 20,
            "output_tokens": 30, "schema_tokens": max(1, visible) * 45,
            "context_window": 20_000,
        })
        calls = self._actions()
        if calls is None:
            return LLMResult(
                content=self._final(), finish_reason="stop",
                input_tokens=150, output_tokens=60,
            )
        self.stage += 1
        return LLMResult(
            content="", tool_calls=calls, finish_reason="tool_calls",
            input_tokens=120, output_tokens=30,
        )


class _ControlledLocalizationModel:
    """A deterministic model policy; all observations still flow through tools."""

    def __init__(self, mode: str, events: list[dict]) -> None:
        self.mode = mode
        self.events = events
        self.stage = 0

    @staticmethod
    def _call(call_id: str, name: str, arguments: dict) -> dict:
        return {
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    async def complete_turn(self, _context, messages, **_kwargs):
        self.events.append({
            "event": "model_call", "input_tokens": 120 + self.stage * 30,
            "output_tokens": 20, "schema_tokens": 60, "context_window": 20_000,
        })
        if self.mode == "off":
            if self.stage == 0:
                calls = [self._call("grep-refresh", "grep_search", {"query": "refresh"})]
            elif self.stage == 1:
                calls = [
                    self._call(f"read-{index}", "read_file", {"path": path})
                    for index, path in enumerate((
                        "app/routes/session.py", "auth/tokens.py", "auth/client.py",
                        "billing/tokens.py",
                    ))
                ]
            else:
                return self._final()
        else:
            if self.stage == 0:
                calls = [self._call("search-symbol", "repo_context", {
                    "operation": "lookup" if self.mode == "lookup" else "symbol_search",
                    "module": "refresh expired",
                })]
            elif self.stage == 1:
                tool_outputs = [
                    item.get("content", "") for item in messages if item.get("role") == "tool"
                ]
                query = "refresh_if_expired"
                if self.mode in {"v3", "lookup"} and tool_outputs:
                    try:
                        payload = json.loads(tool_outputs[-1])
                        if self.mode == "lookup":
                            matches = [
                                item for item in payload.get("items", ())
                                if item.get("kind") == "symbol"
                            ]
                            query = str(matches[0]["identity"])
                        else:
                            matches = payload.get("matches") or []
                            query = str(matches[0]["symbol_id"])
                    except (ValueError, KeyError, IndexError, TypeError):
                        pass
                calls = [self._call("symbol-context", "repo_context", {
                    "operation": "symbol_context", "module": query,
                })]
            elif self.stage == 2:
                paths = ("auth/tokens.py",) if self.mode in {"v3", "lookup"} else (
                    "auth/tokens.py", "billing/tokens.py",
                )
                calls = [
                    self._call(f"read-{index}", "read_file", {"path": path})
                    for index, path in enumerate(paths)
                ]
            else:
                return self._final()
        self.stage += 1
        return LLMResult(
            content="", tool_calls=calls, finish_reason="tool_calls",
            input_tokens=120, output_tokens=20,
        )

    def _final(self) -> LLMResult:
        content = (
            "app/routes/session.py session_route -> auth/tokens.py "
            "refresh_if_expired -> auth/client.py rotate_token. Modify the route "
            "contract, expiry decision, refresh client, and tests/test_token_refresh.py."
        )
        return LLMResult(
            content=content, finish_reason="stop", input_tokens=150, output_tokens=45,
        )


class ControlledRepoIntelligenceDriver:
    """CI driver used to measure OFF/current-name/V3 identity trajectories."""

    evidence_kind = "controlled"

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation:
        events: list[dict] = []
        mode = config.repo_intelligence
        if mode not in {"off", "current", "v3", "lookup"}:
            raise ValueError("repo_intelligence must be off, current, v3, or lookup")
        model = _ControlledLocalizationModel(mode, events)
        tools = _LocalizationTools(workspace, events, config)
        started = time.perf_counter()
        try:
            run = run_native_agent_scenario(
                workspace, prompt=task.prompt, model=model, tools=tools,
                max_turns=config.max_turns,
                tool_names=("grep_search", "read_file", "repo_context"),
                session_id=f"behavior-{task.case_id}-{mode}-{time.time_ns()}",
            )
        finally:
            tools.close()
        result = dict(run["result"])
        events.append({
            "event": "run_complete", "success": result.get("status") == "completed",
            "patch_valid": True,
            "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return BehaviorObservation(
            str(result.get("content") or ""), tuple(events), result,
        )


class ControlledBehaviorDriver:
    """Credential-free A-H driver; the controllable model owns every action."""

    evidence_kind = "controlled"

    def run(
        self, task: BehaviorTask, workspace: Path, config: BehaviorBenchmarkConfig,
    ) -> BehaviorObservation:
        events: list[dict] = []
        tools = _LocalizationTools(workspace, events, config)
        started = time.perf_counter()
        try:
            run = run_native_agent_scenario(
                workspace, prompt=task.prompt,
                model=_ControlledBehaviorModel(task, config, events), tools=tools,
                max_turns=config.max_turns,
                tool_names=(
                    "repo_context", "grep_search", "read_file", "write_file",
                    "bash", "tool_search", "task",
                ),
                session_id=f"behavior-controlled-{task.case_id}-{time.time_ns()}",
            )
        finally:
            tools.close()
        result = dict(run["result"])
        events.append({
            "event": "run_complete", "success": result.get("status") == "completed",
            "patch_valid": True,
            "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return BehaviorObservation(
            str(result.get("content") or ""), tuple(events), result,
        )


def _aggregate_runs(runs: list[dict], *, keys: tuple[str, ...]) -> list[dict]:
    groups: dict[tuple[object, ...], list[dict]] = {}
    for run in runs:
        config = run["config"]
        group = tuple(
            run["task"]["case_id"] if key == "case_id" else config.get(key)
            for key in keys
        )
        groups.setdefault(group, []).append(run)
    metric_names = (
        "turns", "model_calls", "searches", "reads", "duplicate_reads",
        "repo_intelligence_calls", "input_tokens", "output_tokens", "schema_tokens",
        "compactions", "verification_attempts", "verification_recoveries",
        "child_sessions", "conflicts", "wall_time_ms", "cost",
        "semantic_search_calls", "structural_lookup_calls", "retrieval_fallbacks",
        "web_search_calls", "webfetch_calls",
        "localization_turn", "time_to_first_correct_file_ms", "retrieval_precision",
        "ri_candidate_precision",
        "process_start_count", "process_read_count", "process_write_count",
        "process_status_count", "process_resize_count", "process_kill_count",
        "wrong_process_access", "orphan_process_count", "buffer_bytes",
        "process_projection_count",
    )
    result = []
    for group, selected in sorted(groups.items(), key=lambda item: str(item[0])):
        metrics = [run["score"]["metrics"] for run in selected]
        aggregate = {}
        for name in metric_names:
            values = [float(item.get(name) or 0) for item in metrics]
            aggregate[name] = {
                "mean": round(statistics.fmean(values), 3),
                "median": round(float(statistics.median(values)), 3),
            }
        result.append({
            "group": dict(zip(keys, group)), "runs": len(selected),
            "success_rate": round(
                sum(bool(run["score"]["success"]) for run in selected) / len(selected),
                4,
            ),
            "metrics": aggregate,
            "failure_categories": sorted(
                category for run in selected
                if (category := run["trace"].get("failure_category"))
            ),
        })
    return result


def run_repo_intelligence_ab(output_dir: Path) -> dict:
    """Measure localization under OFF/current/V3/unified lookup retrieval."""
    benchmark = AgentBehaviorBenchmark(output_dir, ControlledRepoIntelligenceDriver())
    configs = tuple(
        BehaviorBenchmarkConfig(
            model="controlled-localization-model", reasoning="deterministic",
            repo_intelligence=mode, max_turns=8, token_budget=20_000,
        )
        for mode in ("off", "current", "v3", "lookup")
    )
    result = benchmark.run_matrix(("A",), configs)
    result["comparison"] = {
        run["config"]["repo_intelligence"]: {
            "success": run["score"]["success"],
            "searches": run["score"]["metrics"]["searches"],
            "reads": run["score"]["metrics"]["reads"],
            "repo_intelligence_calls": run["score"]["metrics"]["repo_intelligence_calls"],
            "wrong_file_reads": len(run["score"]["wrong_file_reads"]),
            "turns": run["score"]["metrics"]["turns"],
            "tokens": run["score"]["metrics"]["input_tokens"]
            + run["score"]["metrics"]["output_tokens"],
        }
        for run in result["runs"]
    }
    target = Path(output_dir).resolve() / "repo-intelligence-ab.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_controlled_behavior_suite(output_dir: Path) -> dict:
    """Execute all A-I fixtures through a controllable AgentRunner model."""
    benchmark = AgentBehaviorBenchmark(output_dir, ControlledBehaviorDriver())
    config = BehaviorBenchmarkConfig(
        model="controlled-behavior-model", reasoning="deterministic",
        repo_intelligence="v3", max_turns=30, token_budget=100_000,
    )
    result = benchmark.run_matrix(tuple("ABCDEFGHI"), (config,))
    target = Path(output_dir).resolve() / "controlled-agent-behavior-a-i.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_repo_intelligence_behavior_ab(output_dir: Path) -> dict:
    """Compare OFF/current/V3/lookup on localization and impact tasks."""
    benchmark = AgentBehaviorBenchmark(output_dir, ControlledBehaviorDriver())
    configs = tuple(BehaviorBenchmarkConfig(
        model="controlled-behavior-model", reasoning="deterministic",
        repo_intelligence=mode, max_turns=12,
    ) for mode in ("off", "current", "v3", "lookup"))
    result = benchmark.run_matrix(tuple("ABCD"), configs)
    result["comparison"] = {}
    for mode in ("off", "current", "v3", "lookup"):
        selected = [run for run in result["runs"] if run["config"]["repo_intelligence"] == mode]
        metrics = [run["score"]["metrics"] for run in selected]
        result["comparison"][mode] = {
            "success_rate": sum(bool(run["score"]["success"]) for run in selected) / len(selected),
            "searches": sum(item["searches"] for item in metrics),
            "reads": sum(item["reads"] for item in metrics),
            "repo_intelligence_calls": sum(item["repo_intelligence_calls"] for item in metrics),
            "wrong_file_reads": sum(len(run["score"]["wrong_file_reads"]) for run in selected),
            "turns": sum(item["turns"] for item in metrics),
            "tokens": sum(item["input_tokens"] + item["output_tokens"] for item in metrics),
        }
    target = Path(output_dir).resolve() / "repo-intelligence-behavior-ab.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_controlled_behavior_matrix(output_dir: Path) -> dict:
    """Run the complete provider-free A-I, repo, agent-count, and tool matrices."""
    benchmark = AgentBehaviorBenchmark(output_dir, ControlledBehaviorDriver())
    runs = []
    baseline = BehaviorBenchmarkConfig(
        model="controlled-behavior-model", reasoning="deterministic",
        repo_intelligence="v3", max_turns=30,
    )
    for case_id in "ABCDEFGHI":
        runs.append(benchmark.run_case(case_id, baseline))
    for mode in ("off", "current", "lookup"):
        for case_id in "ABCD":
            runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                model=baseline.model, reasoning=baseline.reasoning,
                repo_intelligence=mode, max_turns=30,
            )))
    for total_agents in (2, 4):
        runs.append(benchmark.run_case("G", BehaviorBenchmarkConfig(
            model=baseline.model, reasoning=baseline.reasoning,
            repo_intelligence="v3", max_turns=30, child_agents=total_agents,
        )))
    for size in (20, 50, 100, 200):
        for progressive in (False, True):
            if size == baseline.tool_catalog_size and progressive == baseline.progressive_exposure:
                continue
            runs.append(benchmark.run_case("H", BehaviorBenchmarkConfig(
                model=baseline.model, reasoning=baseline.reasoning,
                repo_intelligence="v3", max_turns=30,
                tool_catalog_size=size, progressive_exposure=progressive,
            )))
    result = {
        "benchmark_version": 3, "suite_type": "agent-behavior-controlled-matrix",
        "evidence_kind": "controlled",
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / max(1, len(runs)),
    }
    target = Path(output_dir).resolve() / "controlled-agent-behavior-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_behavior_matrix(
    output_dir: Path, *, provider: str, model: str, reasoning: str = "medium",
    temperature: float = 0.0, max_turns: int = 40, repetitions: int = 3,
    case_ids: str = "ABCDEFGHI",
) -> dict:
    """Run repeated real-model RI, recovery, agent, and tool-scale matrices."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    common = {
        "provider": provider, "model": model, "reasoning": reasoning,
        "temperature": temperature, "max_turns": max_turns,
    }
    runs: list[dict] = []
    repeats = max(3, int(repetitions))
    selected_cases = set(str(case_ids).upper())
    for repetition in range(1, repeats + 1):
        for mode in ("off", "current", "v3", "lookup"):
            for case_id in "ABCD":
                if case_id not in selected_cases:
                    continue
                runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                    **common, repo_intelligence=mode, repetition=repetition,
                )))
        for case_id in "EF":
            if case_id not in selected_cases:
                continue
            runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                **common, repo_intelligence="lookup", repetition=repetition,
            )))
        if "G" in selected_cases:
            for total_agents in (1, 2, 4):
                runs.append(benchmark.run_case("G", BehaviorBenchmarkConfig(
                    **common, repo_intelligence="lookup", child_agents=total_agents,
                    repetition=repetition,
                )))
        if "H" in selected_cases:
            for size in (20, 50, 100, 200):
                for progressive in (False, True):
                    runs.append(benchmark.run_case("H", BehaviorBenchmarkConfig(
                        **common, repo_intelligence="lookup", tool_catalog_size=size,
                        progressive_exposure=progressive, repetition=repetition,
                    )))
        if "I" in selected_cases:
            for mode in ("off", "current", "v3", "lookup"):
                runs.append(benchmark.run_case("I", BehaviorBenchmarkConfig(
                    **common, repo_intelligence=mode, repetition=repetition,
                )))
    result = {
        "benchmark_version": 3, "suite_type": "agent-behavior-production-matrix",
        "evidence_kind": "production", "repetitions": repeats,
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / max(1, len(runs)),
        "aggregates": {
            "repo_intelligence": _aggregate_runs(
                [run for run in runs if run["task"]["case_id"] in "ABCD"],
                keys=("case_id", "repo_intelligence"),
            ),
            "core_cases": _aggregate_runs(runs, keys=("case_id",)),
            "multi_agent": _aggregate_runs(
                [run for run in runs if run["task"]["case_id"] == "G"],
                keys=("child_agents",),
            ),
            "tool_scale": _aggregate_runs(
                [run for run in runs if run["task"]["case_id"] == "H"],
                keys=("tool_catalog_size", "progressive_exposure"),
            ),
            "semantic_gate": _aggregate_runs(
                [run for run in runs if run["task"]["case_id"] == "I"],
                keys=("repo_intelligence",),
            ),
        },
        "reference_scores": {"InfCodeX": "unavailable", "OpenCode": "unavailable"},
    }
    target = Path(output_dir).resolve() / "production-agent-behavior-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_verification_matrix(
    output_dir: Path,
    *,
    provider: str,
    model: str,
    reasoning: str = "medium",
    temperature: float = 0.0,
    max_turns: int = 30,
    repetitions: int = 3,
    case_ids: str = "V1,V2,V3,V4,V5,V6,V7,V8,C1,C2,C3,C4",
) -> dict:
    """Run Agent-owned verification, recovery, and completion cases."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    requested = tuple(
        item.strip().upper()
        for item in str(case_ids).replace(";", ",").split(",")
        if item.strip()
    )
    valid = {case_id for case_id, _ in verification_behavior_manifest()}
    selected = tuple(dict.fromkeys(case_id for case_id in requested if case_id in valid))
    if not selected:
        raise ValueError("No verification benchmark cases selected")
    runs: list[dict] = []
    common = {
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "temperature": temperature,
        "max_turns": max_turns,
        "repo_intelligence": "lookup",
        "retrieval_strategy": "guidance",
    }
    repeats = max(3, int(repetitions))
    for repetition in range(1, repeats + 1):
        for case_id in selected:
            runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                **common, repetition=repetition,
            )))
    metric_names = (
        "verification_attempts", "verification_failures",
        "verification_recoveries", "same_failed_command_count",
        "targeted_test_count", "regression_test_count", "turns",
        "wall_time_ms",
    )
    aggregates = []
    for case_id in selected:
        subset = [run for run in runs if run["task"]["case_id"] == case_id]
        metrics = {}
        for name in metric_names:
            values = [float(run["score"]["metrics"].get(name) or 0) for run in subset]
            metrics[name] = {
                "mean": round(statistics.fmean(values), 4),
                "median": round(statistics.median(values), 4),
            }
        aggregates.append({
            "case_id": case_id,
            "runs": len(subset),
            "success_rate": round(sum(bool(run["score"]["success"]) for run in subset) / len(subset), 4),
            "false_pass_count": sum(bool(run["score"]["false_pass"]) for run in subset),
            "false_block_count": sum(bool(run["score"]["false_block"]) for run in subset),
            "degraded_count": sum(run["score"]["runtime_status"] == "completed_unverified" for run in subset),
            "metrics": metrics,
        })
    result = {
        "benchmark_version": 1,
        "suite_type": "agent-verification-reliability-production",
        "evidence_kind": "production",
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "repetitions": repeats,
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / max(1, len(runs)),
        "false_pass_count": sum(bool(run["score"]["false_pass"]) for run in runs),
        "false_block_count": sum(bool(run["score"]["false_block"]) for run in runs),
        "aggregates": aggregates,
    }
    target = Path(output_dir).resolve() / "production-verification-reliability-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_process_matrix(
    output_dir: Path,
    *,
    provider: str,
    model: str,
    reasoning: str = "medium",
    temperature: float = 0.0,
    max_turns: int = 30,
    repetitions: int = 3,
    case_ids: str = "P1,P2,P3,P4,P5,P6",
) -> dict:
    """Run real-model long-running process interaction and cleanup cases."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    requested = tuple(
        item.strip().upper()
        for item in str(case_ids).replace(";", ",").split(",")
        if item.strip()
    )
    valid = {case_id for case_id, _ in process_behavior_manifest()}
    selected = tuple(dict.fromkeys(case_id for case_id in requested if case_id in valid))
    if not selected:
        raise ValueError("No persistent process benchmark cases selected")
    common = {
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "temperature": temperature,
        "max_turns": max_turns,
        "repo_intelligence": "lookup",
        "retrieval_strategy": "guidance",
    }
    repeats = max(3, int(repetitions))
    runs = [
        benchmark.run_case(case_id, BehaviorBenchmarkConfig(
            **common, repetition=repetition,
        ))
        for repetition in range(1, repeats + 1)
        for case_id in selected
    ]
    metric_names = (
        "process_start_count", "process_read_count", "process_write_count",
        "process_status_count", "process_resize_count", "process_kill_count",
        "wrong_process_access", "orphan_process_count", "buffer_bytes",
        "process_projection_count", "turns", "input_tokens", "output_tokens",
        "wall_time_ms",
    )
    aggregates = []
    for case_id in selected:
        subset = [run for run in runs if run["task"]["case_id"] == case_id]
        metrics = {}
        for name in metric_names:
            values = [float(run["score"]["metrics"].get(name) or 0) for run in subset]
            metrics[name] = {
                "mean": round(statistics.fmean(values), 4),
                "median": round(statistics.median(values), 4),
            }
        aggregates.append({
            "case_id": case_id,
            "runs": len(subset),
            "success_rate": round(
                sum(bool(run["score"]["success"]) for run in subset) / len(subset),
                4,
            ),
            "orphan_process_count": sum(
                int(run["score"]["metrics"].get("orphan_process_count") or 0)
                for run in subset
            ),
            "metrics": metrics,
        })
    result = {
        "benchmark_version": 1,
        "suite_type": "agent-persistent-process-production",
        "evidence_kind": "production",
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "repetitions": repeats,
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / len(runs),
        "orphan_process_count": sum(
            int(run["score"]["metrics"].get("orphan_process_count") or 0)
            for run in runs
        ),
        "aggregates": aggregates,
    }
    target = Path(output_dir).resolve() / "production-process-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_web_search_matrix(
    output_dir: Path,
    *,
    provider: str,
    model: str,
    reasoning: str = "medium",
    temperature: float = 0.0,
    max_turns: int = 20,
    repetitions: int = 3,
    case_ids: str = "W1,W2,W3,W4,W5",
) -> dict:
    """Compare real-model external-knowledge behavior with Web Search OFF/ON."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    requested = tuple(
        item.strip().upper()
        for item in str(case_ids).replace(";", ",").split(",")
        if item.strip()
    )
    valid = {case_id for case_id, _ in web_search_behavior_manifest()}
    selected = tuple(dict.fromkeys(case_id for case_id in requested if case_id in valid))
    if not selected:
        raise ValueError("No Web Search benchmark cases selected")
    common = {
        "provider": provider, "model": model, "reasoning": reasoning,
        "temperature": temperature, "max_turns": max_turns,
        "repo_intelligence": "off", "retrieval_strategy": "tool-only",
    }
    repeats = max(3, int(repetitions))
    runs: list[dict] = []
    for repetition in range(1, repeats + 1):
        for enabled in (False, True):
            for case_id in selected:
                runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                    **common, repetition=repetition, web_search_enabled=enabled,
                )))
    aggregates: list[dict] = []
    for enabled in (False, True):
        for case_id in selected:
            subset = [
                run for run in runs
                if run["task"]["case_id"] == case_id
                and bool(run["config"].get("web_search_enabled")) is enabled
            ]
            metrics = [run["score"]["metrics"] for run in subset]
            aggregates.append({
                "case_id": case_id, "web_search_enabled": enabled,
                "runs": len(subset),
                "success_rate": round(sum(bool(run["score"]["success"]) for run in subset) / len(subset), 4),
                "external_evidence_rate": round(sum(
                    run["score"].get("external_evidence_correct") is True for run in subset
                ) / len(subset), 4),
                "unneeded_web_search_count": sum(
                    not bool(run["score"].get("no_unneeded_web")) for run in subset
                ),
                "metrics": {
                    name: {
                        "mean": round(statistics.fmean(float(item.get(name) or 0) for item in metrics), 4),
                        "median": round(statistics.median(float(item.get(name) or 0) for item in metrics), 4),
                    }
                    for name in (
                        "web_search_calls", "webfetch_calls", "turns", "input_tokens",
                        "output_tokens", "schema_tokens", "wall_time_ms",
                    )
                },
            })
    result = {
        "benchmark_version": 1,
        "suite_type": "agent-web-search-production-matrix",
        "evidence_kind": "production", "provider": provider, "model": model,
        "reasoning": reasoning, "repetitions": repeats, "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / len(runs),
        "aggregates": aggregates,
    }
    target = Path(output_dir).resolve() / "production-web-search-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_reference_baseline(
    output_dir: Path,
    *,
    provider: str,
    model: str,
    reasoning: str = "provider-default",
    temperature: float = 0.0,
    max_turns: int = 24,
    repetitions: int = 3,
    case_ids: str = "A,B,E,F,I",
) -> dict:
    """Run nzcoder under the same fixture/budget contract as reference adapters."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    requested = tuple(
        item.strip().upper()
        for item in str(case_ids).replace(";", ",").split(",")
        if item.strip()
    )
    selected = tuple(dict.fromkeys(
        case_id for case_id in requested if case_id in {"A", "B", "E", "F", "I"}
    ))
    if not selected:
        raise ValueError("No reference-comparison cases selected")
    repeats = max(3, int(repetitions))
    runs = []
    for repetition in range(1, repeats + 1):
        for case_id in selected:
            runs.append(benchmark.run_case(case_id, BehaviorBenchmarkConfig(
                provider=provider, model=model, reasoning=reasoning,
                temperature=temperature, max_turns=max_turns,
                repo_intelligence="v3", retrieval_strategy="guidance",
                repetition=repetition,
            )))
    aggregates = []
    for case_id in selected:
        subset = [run for run in runs if run["task"]["case_id"] == case_id]
        aggregates.append({
            "case_id": case_id,
            "runs": len(subset),
            "success_rate": round(
                sum(bool(run["score"]["success"]) for run in subset) / len(subset), 4,
            ),
            "tests_pass_rate": round(
                sum(run["score"]["verification"].get("passed") is not False for run in subset)
                / len(subset), 4,
            ),
            "mean_wall_time_ms": round(statistics.fmean(
                float(run["score"]["metrics"].get("wall_time_ms") or 0)
                for run in subset
            ), 3),
        })
    result = {
        "benchmark_version": 1,
        "suite_type": "nzcoder-reference-comparison-baseline",
        "evidence_kind": "production",
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "max_turns": max_turns,
        "repetitions": repeats,
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / len(runs),
        "aggregates": aggregates,
    }
    target = Path(output_dir).resolve() / "nzcoder-reference-comparison-baseline.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def run_production_retrieval_matrix(
    output_dir: Path, *, provider: str, model: str, reasoning: str = "medium",
    temperature: float = 0.0, max_turns: int = 40, repetitions: int = 3,
    case_ids: str = "A,B,C,D,I,I2,I3,I4,IS", semantic_model: str = "",
    include_structural: bool = True, resume: bool = False,
) -> dict:
    """Compare tool-only, guidance, bounded auto context, policy, and embeddings."""
    benchmark = AgentBehaviorBenchmark(output_dir, ProductionAgentBehaviorDriver())
    common = {
        "provider": provider, "model": model, "reasoning": reasoning,
        "temperature": temperature, "max_turns": max_turns,
        "repo_intelligence": "lookup",
    }
    raw_cases = str(case_ids).upper().replace(";", ",")
    requested = (
        [item.strip() for item in raw_cases.split(",") if item.strip()]
        if "," in raw_cases else list(raw_cases)
    )
    retrieval_cases = {"A", "B", "C", "D", "I", "I2", "I3", "I4", "IS"}
    selected = tuple(
        case_id for case_id in dict.fromkeys(requested)
        if case_id in retrieval_cases
    )
    runs: list[dict] = []
    repeats = max(3, int(repetitions))
    existing: dict[tuple[int, str, str, str], dict] = {}
    if resume:
        report_dir = Path(output_dir).resolve() / "reports"
        for report in sorted(report_dir.glob("*.json")) if report_dir.exists() else ():
            try:
                run = json.loads(report.read_text(encoding="utf-8"))
                config = run["config"]
                case_id = str(run["task"]["case_id"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not (
                str(config.get("provider") or "") == provider
                and str(config.get("model") or "") == model
                and str(config.get("reasoning") or "") == reasoning
                and float(config.get("temperature") or 0.0) == float(temperature)
                and int(config.get("max_turns") or 0) == int(max_turns)
                and str(config.get("repo_intelligence") or "") == "lookup"
                and case_id in selected
            ):
                continue
            key = (
                int(config.get("repetition") or 0),
                str(config.get("retrieval_strategy") or ""), case_id,
                str(config.get("semantic_model") or ""),
            )
            existing[key] = run
    reused_runs = 0

    def collect(case_id: str, config: BehaviorBenchmarkConfig) -> None:
        nonlocal reused_runs
        key = (
            int(config.repetition), str(config.retrieval_strategy), case_id,
            str(config.semantic_model or ""),
        )
        prior = existing.get(key)
        if prior is not None:
            runs.append(prior)
            reused_runs += 1
            return
        runs.append(benchmark.run_case(case_id, config))

    for repetition in range(1, repeats + 1):
        if include_structural:
            for strategy in ("tool-only", "guidance", "auto-context", "policy"):
                for case_id in selected:
                    collect(case_id, BehaviorBenchmarkConfig(
                        **common, retrieval_strategy=strategy, repetition=repetition,
                    ))
        if semantic_model:
            for strategy in ("tool-only", "policy"):
                for case_id in selected:
                    if case_id not in {"A", "D", "I", "I2", "I3", "I4", "IS"}:
                        continue
                    collect(case_id, BehaviorBenchmarkConfig(
                        **common, retrieval_strategy=strategy,
                        semantic_model=semantic_model, repetition=repetition,
                    ))
    result = {
        "benchmark_version": 4,
        "suite_type": "agent-behavior-production-retrieval-matrix",
        "evidence_kind": "production", "repetitions": repeats,
        "semantic_model": semantic_model or None,
        "includes_structural_controls": bool(include_structural),
        "resumed": bool(resume), "reused_runs": reused_runs,
        "runs": runs,
        "success_rate": sum(bool(run["score"]["success"]) for run in runs) / max(1, len(runs)),
        "aggregates": {
            "retrieval_strategy": _aggregate_runs(
                runs, keys=("case_id", "retrieval_strategy", "semantic_model"),
            ),
        },
        "reference_scores": {"InfCodeX": "unavailable", "OpenCode": "unavailable"},
    }
    target = Path(output_dir).resolve() / "production-retrieval-matrix.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


__all__ = [
    "AgentBehaviorBenchmark", "AgentRunnerBehaviorDriver", "BehaviorBenchmarkConfig",
    "BehaviorDriver", "BehaviorObservation", "BehaviorTask", "CallableBehaviorDriver",
    "ControlledBehaviorDriver", "ControlledRepoIntelligenceDriver",
    "ProductionAgentBehaviorDriver", "behavior_manifest",
    "run_controlled_behavior_matrix", "run_controlled_behavior_suite",
    "run_production_behavior_matrix",
    "run_production_verification_matrix",
    "run_production_process_matrix",
    "run_production_web_search_matrix",
    "run_production_reference_baseline",
    "run_production_retrieval_matrix",
    "run_repo_intelligence_ab", "run_repo_intelligence_behavior_ab",
    "process_behavior_manifest", "verification_behavior_manifest", "web_search_behavior_manifest",
]
