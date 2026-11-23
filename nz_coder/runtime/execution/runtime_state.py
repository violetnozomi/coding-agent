"""RuntimeState — Claude Code 风格的 state-as-message 机制。

在每个 agent loop 迭代中跟踪客观事实状态（turn、diff、验证、空转），
并在每轮 LLM 调用前注入 <system-reminder> 状态块，让模型基于当前状态
做出更合理的探索/收敛决策。

与 scratchpad 的区分：
  - scratchpad = agent 主观推理笔记（agent 自己写）
  - RuntimeState = 系统自动跟踪的客观事实（工具调用后自动更新）
"""

from __future__ import annotations

from nz_coder.runtime.conversation.runtime_checkpoint import build_runtime_checkpoint, write_runtime_snapshot

import json
import math
import re
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path

from nz_coder.runtime.verification.reference_evidence import (
    capture_references, extend_references, observe_reference_read, sanitize_references,
)
from nz_coder.runtime.agent.task_policy import (
    is_broad_test_command,
    is_documentation_file,
    is_exact_test_command,
    is_test_file,
    explicit_instruction_paths,
    explicit_read_only_paths,
    extract_explicit_mutation_operations,
    successful_mutation_operations,
    task_forbids_test_changes,
    task_wants_tests,
    update_ephemeral_scratch_lifecycle,
)
from nz_coder.runtime.verification.verification_contract import (
    VerificationContract,
    effective_acceptance_generation,
    extract_verification_contract,
)
from nz_coder.tools import (
    collect_filesystem_mutation_paths,
    is_filesystem_mutation_tool,
)


def verification_snapshot_with_paths(state):
    """在同一把运行锁内确定证据的版本和作用路径，避免把下一代修改拼入旧快照。"""
    context = getattr(state, "_verification_context", None)
    with context._state_lock if context is not None else nullcontext():
        domain = getattr(state, "verification_domain", None)
        return (domain.snapshot() if domain is not None else None,
                tuple(getattr(state, "changed_files", ())))


STRICT_INVESTIGATION_SOFT_LIMIT = 12
# Deprecated compatibility symbol. Investigation policy no longer consults it;
# convergence is advisory until the independent total work budget is exhausted.
STRICT_INVESTIGATION_HARD_LIMIT = 20
PROVIDER_TURN_RECORD_LIMIT = 200
_INVESTIGATION_TOOLS = frozenset({
    "grep_search",
    "glob_search",
    "read_file",
    "read_symbol",
    "list_directory",
    "repo_map",
    "code_references",
    "find_symbol_callers",
    "analyze_impact",
})
_BASH_INVESTIGATION_COMMANDS = frozenset({
    "cat", "grep", "head", "rg", "sed", "tail", "tree", "find",
})
_EXPLICIT_TASK_LIST_REQUEST_RE = re.compile(
    r"(?:\b(?:use|maintain|create|keep|show|update)\s+"
    r"(?:a\s+|the\s+)?(?:todo(?:\s+(?:list|checklist))?|checklist|task\s+list)\b)"
    r"|(?:(?:使用|维护|创建|保留|显示|更新).{0,4}(?:待办|任务清单|检查清单))",
    re.IGNORECASE,
)


def _closure_path(value) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_read_only_git_observation(command: str) -> bool:
    """Return whether shell input is a safe local Git diff/status inspection."""
    compact = " ".join(str(command or "").split())
    if re.match(r"^git\s+(?:diff|status)(?:\s|$)", compact) is None:
        return False
    from nz_coder.tool_platform.command_policy import is_known_read_only_command

    return is_known_read_only_command(compact)


def _is_source_mutation_path(value: str) -> bool:
    """Return whether a write can change product behavior rather than tests/docs."""
    path = _closure_path(value).lower()
    if not path or is_test_file(path):
        return False
    if path.endswith((".md", ".rst", ".txt")):
        return False
    return not any(part in {"docs", "doc"} for part in path.split("/")[:-1])


def _successful_file_scoped_grep(
    tool_input: dict,
    output: str,
    succeeded: bool | None,
) -> str:
    """Return an exact file path when content grep produced usable evidence."""
    path = _closure_path(tool_input.get("path"))
    rendered = str(output or "").strip()
    if (
        succeeded is False
        or str(tool_input.get("output_mode") or "content") != "content"
        or not path
        or not Path(path).suffix
        or not rendered
        or rendered == "No files found"
        or rendered.startswith(("Error:", "Denied"))
    ):
        return ""
    return path if is_test_file(path) or _is_source_mutation_path(path) else ""


def _successful_exact_read(
    name: str,
    tool_input: dict,
    output: str,
    succeeded: bool | None,
) -> str:
    """Return exact file evidence produced by a successful content read."""
    path = _closure_path(tool_input.get("path"))
    rendered = str(output or "").strip()
    if (
        succeeded is False
        or not path
        or not Path(path).suffix
        or not rendered
        or rendered.startswith(("Error:", "Denied"))
    ):
        return ""
    if name == "read_file" and "<type>file</type>" not in rendered:
        return ""
    if name == "read_symbol" and re.match(
        r"^symbol\s+.+?\s+not found in\s+", rendered, re.IGNORECASE,
    ):
        return ""
    return path if is_test_file(path) or _is_source_mutation_path(path) else ""


def _nonnegative_finite_float(value) -> float | None:
    """Normalize one persisted monetary value without accepting booleans."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        return None
    return normalized


def _sanitize_cost_map(value) -> dict[str, float]:
    """Return the valid finite entries from a persisted cost dimension."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, float] = {}
    for raw_key, raw_cost in value.items():
        key = str(raw_key or "").strip()
        cost = _nonnegative_finite_float(raw_cost)
        if key and cost is not None:
            normalized[key] = round(cost, 12)
    return normalized


def _sanitize_counter_map(value) -> dict[str, int]:
    """Return non-negative integer counters from persisted telemetry."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        key = str(raw_key or "").strip()
        if not key or isinstance(raw_count, bool):
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError, OverflowError):
            continue
        if count >= 0:
            normalized[key] = count
    return normalized


def _nonnegative_int(value, *, default: int = 0) -> int:
    """Normalize one persisted counter without letting booleans masquerade."""
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return normalized if normalized >= 0 else default


def _signed_int(value, *, default: int = -1) -> int:
    """Normalize a persisted signed counter without accepting booleans."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _string_list(value, *, limit: int) -> list[str]:
    """Return one bounded list from an untrusted persisted collection."""
    if not isinstance(value, list):
        return []
    normalized = [
        item for item in value
        if isinstance(item, str) and item.strip()
    ]
    return normalized[-limit:]


def _sanitize_usage(value) -> dict[str, int]:
    """Normalize a Provider usage bucket to the runtime's fixed schema."""
    keys = ("input", "output", "total", "reasoning", "cache_read", "cache_write")
    source = value if isinstance(value, dict) else {}
    normalized = {key: _nonnegative_int(source.get(key)) for key in keys}
    component_total = sum(
        normalized[key]
        for key in ("input", "output", "reasoning", "cache_read", "cache_write")
    )
    # Current Gateway records already contain mutually exclusive buckets and a
    # normalized total.  Preserve valid historical totals exactly; only rebuild
    # a missing/corrupt total so loading a snapshot remains round-trip stable.
    if not normalized["total"] and component_total:
        normalized["total"] = component_total
    return normalized


def _sanitize_nested_usage(value) -> dict[str, dict[str, int]]:
    """Normalize purpose/model keyed usage dimensions."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for raw_key, raw_usage in value.items():
        key = str(raw_key or "").strip()
        if key and isinstance(raw_usage, dict):
            normalized[key] = _sanitize_usage(raw_usage)
    return normalized


def _sanitize_duration_map(value) -> dict[str, float]:
    """Normalize finite, non-negative Provider duration dimensions."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, float] = {}
    for raw_key, raw_duration in value.items():
        key = str(raw_key or "").strip()
        duration = _nonnegative_finite_float(raw_duration)
        if key and duration is not None:
            normalized[key] = round(duration, 3)
    return normalized


def _normalize_provider_turn_record(record) -> dict | None:
    """Return one bounded, typed Provider turn record, if structurally valid."""
    if not isinstance(record, dict):
        return None
    reason = str(record.get("reason") or "unknown").strip() or "unknown"
    outcome = str(record.get("outcome") or "unknown").strip() or "unknown"
    raw_tool_names = record.get("tool_names")
    tool_names = raw_tool_names if isinstance(raw_tool_names, (list, tuple)) else ()
    verification_generation = record.get("verification_generation_after", -1)
    if isinstance(verification_generation, bool):
        verification_generation = -1
    try:
        verification_generation = int(verification_generation)
    except (TypeError, ValueError, OverflowError):
        verification_generation = -1
    return {
        "turn": max(1, _nonnegative_int(record.get("turn"), default=1)),
        "reason": reason,
        "outcome": outcome,
        "tool_names": [str(name) for name in tool_names[:16] if str(name)],
        "finish_reason": str(record.get("finish_reason") or ""),
        "mutation_generation_before": _nonnegative_int(
            record.get("mutation_generation_before")
        ),
        "mutation_generation_after": _nonnegative_int(
            record.get("mutation_generation_after")
        ),
        "mutation_delta": _nonnegative_int(record.get("mutation_delta")),
        "verification_generation_after": verification_generation,
    }


# ── Broad test runner 检测 — 识别"跑全套测试"的行为 ──────────────────────────

def _is_broad_test_command(command: str) -> bool:
    """向后兼容包装：实际规则在 task_policy 中维护。"""
    return is_broad_test_command(command)


def _is_exact_test(command: str) -> bool:
    """向后兼容包装：实际规则在 task_policy 中维护。"""
    return is_exact_test_command(command)


# ═══════════════════════════════════════════════════════════════════════════════
# RuntimeState
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RuntimeState:
    """持有执行事实与模型判断；task_assessment_state不具授权或验证权威。"""

    # ── Turn / Time ──────────────────────────────────────────────────────────
    turn_count: int = 0
    max_turns: int = 80
    started_at: float = 0.0
    timeout_seconds: int = 0

    # ── Edit tracking ────────────────────────────────────────────────────────
    last_edit_turn: int = 0           # 最后一轮做了源码编辑的 turn 号
    edits_this_run: int = 0           # 本次 run 共做了多少次编辑

    # ── Diff status（由 diff_status 工具调用更新）─────────────────────────────
    has_diff: bool = False
    diff_chars: int = 0
    changed_files: list[str] = field(default_factory=list)
    tests_modified: bool = False
    source_only: bool = False

    # ── L1 acceptance criteria ───────────────────────────────────────────────
    acceptance_criteria: list[str] = field(default_factory=list)
    requested_paths: list[str] = field(default_factory=list)
    task_assessment_state: dict = field(default_factory=dict)
    wants_tests: bool = False
    forbids_test_changes: bool = False

    # ── Planning tracking ───────────────────────────────────────────────────
    plan_generated: bool = False
    plan_text: str = ""
    plan_revision: int = 0
    plan_last_commit: dict = field(default_factory=dict)
    execution_handoff: dict = field(default_factory=dict)
    open_todo_items: int = 0
    task_reference_evidence: list[dict] = field(default_factory=list)
    task_references_bound: bool = False
    task_authority_epoch: int = 0
    task_authority_message_id: str = ""
    task_reference_omitted_count: int = 0
    task_contract: dict = field(default_factory=dict)
    workspace_observation: dict = field(default_factory=dict)
    completion_gate_prompts: int = 0
    completion_gate_signature: str = ""
    initial_task_text: str = ""
    current_round_instruction_text: str = ""
    patch_risk: dict = field(default_factory=dict)
    risk_feedback_fingerprint: str = ""

    # ── Verification tracking ────────────────────────────────────────────────
    verification_attempts: int = 0    # 总验证尝试次数
    broad_test_attempts: int = 0     # 跑了多少次 broad test runner
    exact_test_attempts: int = 0     # 跑了多少次精确测试
    verification_definition: dict = field(default_factory=dict)
    verification_failures: int = 0
    completion_review_rejections: int = 0
    last_completion_review_rejection: str = ""
    completion_review_generation: int = -1
    needs_broad_exploration: bool = False
    scheduled_verification_generations: dict[str, int] = field(default_factory=dict)
    primary_recovery_classification: str = ""
    supporting_recovery_classifications: list[str] = field(default_factory=list)
    recovery_repair_targets: list[str] = field(default_factory=list)

    # ── Search / Read tracking（检测空转）─────────────────────────────────────
    searched_patterns: list[str] = field(default_factory=list)
    read_files: list[str] = field(default_factory=list)
    investigation_calls_since_edit: int = 0
    mutation_generation: int = 0
    last_mutation_paths: tuple[str, ...] = ()
    source_mutation_generation: int = 0
    acceptance_mutation_generation: int | None = None
    diff_generation: int = -1
    strict_progress_nudges: int = 0
    strict_progress_blocks: int = 0
    budget_zones_emitted: list[str] = field(default_factory=list)
    budget_pressure_zone: str = "green"
    work_phase: str = "normal"
    workspace_git_available: bool | None = None
    package_install_attempts: int = 0
    emergency_broad_exploration: int = 0

    # ── Complete Provider accounting (coding + control plane) ───────────────
    provider_calls: int = 0
    provider_attempts: int = 0
    provider_calls_by_purpose: dict[str, int] = field(default_factory=dict)
    provider_calls_by_model: dict[str, int] = field(default_factory=dict)
    provider_usage: dict[str, int] = field(default_factory=dict)
    provider_usage_by_purpose: dict[str, dict[str, int]] = field(default_factory=dict)
    provider_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    provider_duration_ms_by_purpose: dict[str, float] = field(default_factory=dict)
    provider_duration_ms_by_model: dict[str, float] = field(default_factory=dict)
    provider_cost_usd: float = 0.0
    provider_cost_usd_by_purpose: dict[str, float] = field(default_factory=dict)
    provider_cost_usd_by_model: dict[str, float] = field(default_factory=dict)
    provider_cost_unknown_calls: int = 0
    provider_cost_sources: dict[str, int] = field(default_factory=dict)
    provider_turn_records: list[dict] = field(default_factory=list)
    provider_turns_by_reason: dict[str, int] = field(default_factory=dict)
    provider_turns_by_outcome: dict[str, int] = field(default_factory=dict)

    # ── Transition ────────────────────────────────────────────────────────────
    # 上轮做了什么，用于判断当前应该收敛还是继续探索
    # "edited_source" | "ran_broad_test" | "ran_exact_test" | "searched" | "read" | ""
    transition: str = ""

    # ── 标志 ──────────────────────────────────────────────────────────────────
    _state_block_emitted: bool = False
    _diff_seen_from_tool: bool = False   # diff_status 被调用过

    # ═══════════════════════════════════════════════════════════════════════════

    def __post_init__(self) -> None:
        # Sidecar judges settle on helper threads.  This lock is deliberately
        # runtime-local and dynamic so dataclass serialization never sees it.
        self._provider_accounting_lock = threading.RLock()
        self._persistence_lock = threading.RLock()
        self._ephemeral_scratch_paths: set[str] = set()
        self._verification_context = None
        self._restored_verification_domain = None

    def reset(self, max_turns: int = 80, timeout_seconds: int = 0):
        """在每次 run() 开始时调用。"""
        self._restored_verification_domain = None
        self.workspace_observation = {}
        self.turn_count = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.timeout_seconds = timeout_seconds

        self.last_edit_turn = 0
        self.edits_this_run = 0

        self.has_diff = False
        self.diff_chars = 0
        self.changed_files = []
        self.tests_modified = False
        self.source_only = False
        self.acceptance_criteria = []
        self.requested_paths = []
        self.task_assessment_state = {}
        self.wants_tests = False
        self.forbids_test_changes = False

        self.plan_generated = False
        self.plan_text = ""
        self.plan_revision = 0
        self.plan_last_commit = {}
        self.execution_handoff = {}
        self.open_todo_items = 0
        self.task_reference_evidence = []
        self.task_references_bound = False
        self.task_authority_epoch = 0
        self.task_authority_message_id = ""
        self.task_reference_omitted_count = 0
        self.task_contract = {}
        self.completion_gate_prompts = 0
        self.completion_gate_signature = ""
        self.initial_task_text = ""
        self.current_round_instruction_text = ""
        self.patch_risk = {}
        self.risk_feedback_fingerprint = ""

        self.verification_attempts = 0
        self.broad_test_attempts = 0
        self.exact_test_attempts = 0
        self.verification_definition = {}
        self.verification_failures = 0
        self.completion_review_rejections = 0
        self.last_completion_review_rejection = ""
        self.completion_review_generation = -1
        self.needs_broad_exploration = False
        self.scheduled_verification_generations = {}
        self.primary_recovery_classification = ""
        self.supporting_recovery_classifications = []
        self.recovery_repair_targets = []

        self.searched_patterns = []
        self.read_files = []
        self.investigation_calls_since_edit = 0
        self.mutation_generation = 0
        self.last_mutation_paths = ()
        self.source_mutation_generation = 0
        self.acceptance_mutation_generation = 0
        self.diff_generation = -1
        self.strict_progress_nudges = 0
        self.strict_progress_blocks = 0
        self.budget_zones_emitted = []
        self.budget_pressure_zone = "green"
        self.work_phase = "normal"
        self.workspace_git_available = None
        self.package_install_attempts = 0
        self.emergency_broad_exploration = 0
        self.provider_calls = 0
        self.provider_attempts = 0
        self.provider_calls_by_purpose = {}
        self.provider_calls_by_model = {}
        self.provider_usage = {}
        self.provider_usage_by_purpose = {}
        self.provider_usage_by_model = {}
        self.provider_duration_ms_by_purpose = {}
        self.provider_duration_ms_by_model = {}
        self.provider_cost_usd = 0.0
        self.provider_cost_usd_by_purpose = {}
        self.provider_cost_usd_by_model = {}
        self.provider_cost_unknown_calls = 0
        self.provider_cost_sources = {}
        self.provider_turn_records = []
        self.provider_turns_by_reason = {}
        self.provider_turns_by_outcome = {}

        self.transition = ""
        self._state_block_emitted = False
        self._diff_seen_from_tool = False
        self._ephemeral_scratch_paths = set()

    def set_acceptance_criteria_from_text(self, text: str, limit: int = 5, *, workspace=None) -> None:
        """从用户任务文本中提取轻量 L1 验收标准。"""
        self.acceptance_criteria = extract_acceptance_criteria(text, limit=limit)
        self.requested_paths = extract_explicit_mutation_paths(text, limit=20)
        self.wants_tests = task_wants_tests(text)
        self.forbids_test_changes = task_forbids_test_changes(text)
        contract = extract_verification_contract(text)
        self.verification_definition = {"command": contract.command, "targets": list(contract.targets)} if contract else {}
        from nz_coder.runtime.agent.task_contract import derive_task_contract
        from nz_coder.runtime.verification.verification_contract import extract_verification_contracts
        self.set_task_contract(derive_task_contract(text, workspace=workspace,
            acceptance_commands=tuple(item.command for item in extract_verification_contracts(text))))

    def apply_current_round_instruction(
        self,
        text: str,
        limit: int = 5,
        *,
        workspace: str | Path | None = None,
        source_message_id: str = "",
        capture_task_references: bool = True,
    ) -> None:
        """Overlay explicit follow-up constraints without replacing task history."""
        from nz_coder.protocol.message_schema import is_synthetic_user_message

        if not str(text or "").strip() or is_synthetic_user_message({"role": "user", "content": text}):
            return
        if workspace is not None and capture_task_references:
            self.extend_task_references_from_user_instruction(
                {"role": "user", "content": text, "_nz_message_id": source_message_id},
                workspace=workspace,
            )
        self.current_round_instruction_text = str(text or "").strip()[:4000]
        current_criteria = extract_acceptance_criteria(text, limit=limit)
        self.acceptance_criteria = _prioritized_unique(
            current_criteria,
            self.acceptance_criteria,
            limit=limit,
        )
        current_paths = extract_explicit_mutation_paths(text, limit=20)
        self.requested_paths = _prioritized_unique(
            current_paths,
            self.requested_paths,
            limit=20,
        )

        self.apply_current_round_test_preferences(text)
        forbids_test_changes = task_forbids_test_changes(text)
        wants_tests = task_wants_tests(text)
        allow_test_changes = False if self.forbids_test_changes else True if wants_tests else None

        from nz_coder.runtime.verification.verification_contract import extract_verification_contracts
        contracts = extract_verification_contracts(text)
        if contracts:
            contract = contracts[0]
            self.verification_definition = {"command": contract.command, "targets": list(contract.targets)}
            self._merge_current_round_contract(
                text,
                tuple(item.command for item in contracts),
                workspace=workspace,
                explicit_paths=tuple(current_paths),
                allow_test_changes=allow_test_changes,
            )
        else:
            mutation_paths = extract_explicit_mutation_paths(text, limit=20)
            if mutation_paths or self.task_contract or forbids_test_changes or explicit_read_only_paths(text):
                self._merge_current_round_artifacts(
                    text,
                    workspace=workspace,
                    explicit_paths=tuple(mutation_paths),
                    allow_test_changes=allow_test_changes,
                )
        readonly = {path for clause in self.task_contract.get("constraints", ())
                    for path in explicit_read_only_paths(clause)}
        self.requested_paths = [path for path in self.requested_paths
                                if path not in readonly and not (self.forbids_test_changes and is_test_file(path))]

    def apply_current_round_test_preferences(self, text: str) -> None:
        """独立更新测试编辑偏好，避免新任务重新合并撤销前言的路径。"""
        if task_forbids_test_changes(text):
            self.forbids_test_changes = True
            self.wants_tests = False
        elif task_wants_tests(text):
            self.forbids_test_changes = False
            self.wants_tests = True

    def _merge_current_round_artifacts(
        self,
        text: str,
        *,
        workspace: str | Path | None,
        explicit_paths: tuple[str, ...],
        allow_test_changes: bool | None = None,
    ) -> None:
        """Add hard write facts without inventing semantic behavior criteria."""
        from nz_coder.runtime.agent.task_contract import derive_round_artifact_contract, merge_round_task_contract, parse_task_contract
        from nz_coder.runtime.verification.domain.requirements import TaskContract

        try:
            if self.task_contract:
                current = parse_task_contract(
                    self.task_contract,
                    workspace=workspace,
                )
            else:
                current = TaskContract(
                    objective=str(self.initial_task_text or text).strip()[:2000]
                )
            round_contract = derive_round_artifact_contract(
                text,
                artifact_paths=explicit_paths,
                workspace=workspace,
            )
        except (TypeError, ValueError):
            return
        merged_contract = merge_round_task_contract(current, round_contract,
            allow_test_changes=allow_test_changes, allow_write_paths=explicit_paths)
        self.set_task_contract(merged_contract)

    def _merge_current_round_contract(
        self,
        text: str,
        commands: tuple[str, ...],
        *,
        workspace: str | Path | None,
        explicit_paths: tuple[str, ...],
        allow_test_changes: bool | None,
    ) -> None:
        """Extend an existing deterministic ledger with explicit round work."""
        from nz_coder.runtime.agent.task_contract import derive_task_contract, merge_round_task_contract, parse_task_contract
        from nz_coder.runtime.verification.domain.requirements import TaskContract

        try:
            current = parse_task_contract(
                self.task_contract,
                workspace=workspace,
            ) if self.task_contract else TaskContract(objective=str(self.initial_task_text or text).strip()[:2000])
            round_contract = derive_task_contract(
                text,
                acceptance_commands=commands,
                workspace=workspace,
                artifact_allowlist=explicit_paths,
                explicit_path_allowlist=explicit_paths,
            )
        except (TypeError, ValueError):
            return
        merged_contract = merge_round_task_contract(
            current,
            round_contract,
            allow_test_changes=allow_test_changes,
            allow_write_paths=explicit_paths,
        )
        self.set_task_contract(merged_contract)

    def record_recovery_diagnostic(self, diagnostic: str) -> None:
        """Persist structured failure facts for later closure decisions."""
        self.primary_recovery_classification = ""
        self.supporting_recovery_classifications = []
        self.recovery_repair_targets = []
        primary = ""
        supporting: list[str] = []
        targets: list[str] = []
        for raw_line in str(diagnostic or "").splitlines():
            key, separator, value = raw_line.partition(":")
            if not separator:
                continue
            normalized = value.strip()
            if not normalized:
                continue
            if key.strip() == "primary_classification":
                primary = normalized
            elif key.strip() == "supporting_classification":
                if normalized not in supporting:
                    supporting.append(normalized)
            elif key.strip() == "repair_target":
                target = _closure_path(normalized)
                if target and target not in targets:
                    targets.append(target)
        if primary:
            self.primary_recovery_classification = primary
        self.supporting_recovery_classifications = supporting
        self.recovery_repair_targets = targets
        if primary in {
            "subprocess_package_root",
            "subprocess_workspace_drift",
        } and targets:
            # These classifications already identify a concrete test helper;
            # the generic ``No module named`` marker must not revoke the
            # evidence-backed closure reserve as if repository-wide discovery
            # were still required.
            self.needs_broad_exploration = False

    def bind_task_references(self, task: str, *, workspace: str | Path) -> None:
        """Called only by fresh genuine-task lifecycle, before model/tools run."""
        if self.task_references_bound:
            return
        refs, omitted = capture_references(task, workspace, self.mutation_generation)
        self.task_reference_evidence = [ref.to_dict() for ref in refs]
        self.task_reference_omitted_count = omitted
        self.task_references_bound = True

    def extend_task_references_from_user_instruction(self, message: dict, *, workspace: str | Path) -> None:
        """Only lifecycle genuine messages may extend authority; never reset initial binding."""
        from nz_coder.protocol.message_schema import is_synthetic_user_message

        if (not isinstance(message, dict) or message.get("role") != "user"
                or is_synthetic_user_message(message)):
            return
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            return
        identity = message.get("_nz_message_id", "")
        if not isinstance(identity, str) or (identity and not re.fullmatch(r"msg-[A-Za-z0-9_-]{1,128}", identity)):
            return
        if identity and (identity == self.task_authority_message_id or any(
                ref.get("source_message_id") == identity for ref in self.task_reference_evidence)):
            return
        if self.task_authority_epoch >= 2**63 - 1:
            return  # Corrupt/exhausted epoch cannot wrap into historical authority.
        self.task_authority_epoch += 1
        refs, omitted = extend_references(
            self.task_reference_evidence, text, workspace, self.mutation_generation,
            authority_epoch=self.task_authority_epoch, source_message_id=identity,
        )
        self.task_reference_evidence = [ref.to_dict() for ref in refs]
        self.task_reference_omitted_count += omitted
        self.task_authority_message_id = identity

    def observe_task_reference_read(self, metadata: dict | None) -> None:
        from nz_coder.runtime.process.workdir import current_workdir

        self.task_reference_evidence = [ref.to_dict() for ref in observe_reference_read(
            self.task_reference_evidence, metadata, current_workdir(),
        )]

    def set_task_contract(self, contract) -> None:
        """宿主提交完整当前定义；模型规划没有此入口的调用权。"""
        from contextlib import nullcontext
        from nz_coder.runtime.verification.domain.requirements import TaskContract
        from nz_coder.runtime.verification.domain.models import TaskDefinition

        context = self._verification_context
        with context._state_lock if context is not None else nullcontext():
            domain = self.verification_domain
            if not isinstance(contract, TaskContract):
                raise TypeError("host task contract must be a TaskContract")
            if domain is not None:
                domain.declare_task(TaskDefinition(domain.scope.task, self.task_authority_epoch,
                                                   self.task_authority_message_id, contract))
                contract = domain.task_definition.contract
            self.task_contract = contract.to_dict()
            from nz_coder.runtime.agent.task_policy import test_command_targets
            first = contract.acceptance_commands[0] if contract.acceptance_commands else ""
            self.verification_definition = {"command": first, "targets": list(test_command_targets(first))} if first else {}

    def contract_owns_progress(self) -> bool:
        """Return whether the Runtime ledger is the sole plan-state owner."""
        requirements = self.task_contract.get("requirements", [])
        if not isinstance(requirements, list) or not requirements:
            return False
        return _EXPLICIT_TASK_LIST_REQUEST_RE.search(
            str(self.initial_task_text or "")
        ) is None

    def requirement_ledger_snapshot(self):
        """需求状态只从域读取；没有执行事实时只能得到待办投影。"""
        from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView
        from nz_coder.runtime.agent.task_contract import parse_task_contract
        domain = self.verification_domain
        if domain is not None:
            return RequirementLedgerView.from_snapshot(domain.snapshot())
        return RequirementLedgerView.from_contract(parse_task_contract(self.task_contract))

    def observe_completion_review_rejection(self, reason: str) -> None:
        """Record objective review feedback that authorizes bounded repair work.

        A StopHook/CompletionGate rejection is verification evidence even when
        the declared test command passed.  Keeping it separate from command
        failures preserves accurate diagnostics while allowing the nominal-SLA
        gate to spend the existing hard-cap reserve on the requested repair.
        """
        self.completion_review_rejections += 1
        self.last_completion_review_rejection = str(reason or "")[-4000:]

    def semantic_review_pending_only(self) -> bool:
        """Return whether semantic review is the sole missing contract fact."""
        return bool(
            self.requirement_ledger
            and self.requirement_ledger_snapshot().semantic_review_pending_only(
                acceptance_required=bool(self.verification_contract),
                generation=effective_acceptance_generation(self),
            )
        )

    def observe_provider_call(
        self,
        purpose: str,
        *,
        usage: dict | None,
        attempts: int,
        duration_ms: float,
        provider_id: str = "",
        model_id: str = "",
        cost: float | None = None,
        cost_source: str | None = None,
    ) -> None:
        """Aggregate a concurrent-safe settled Provider call."""
        with self._provider_accounting_lock:
            self._observe_provider_call_unlocked(
                purpose,
                usage=usage,
                attempts=attempts,
                duration_ms=duration_ms,
                provider_id=provider_id,
                model_id=model_id,
                cost=cost,
                cost_source=cost_source,
            )

    def _observe_provider_call_unlocked(
        self,
        purpose: str,
        *,
        usage: dict | None,
        attempts: int,
        duration_ms: float,
        provider_id: str = "",
        model_id: str = "",
        cost: float | None = None,
        cost_source: str | None = None,
    ) -> None:
        """Aggregate one settled logical Provider call by diagnostic purpose."""
        normalized_purpose = str(purpose or "unknown").strip() or "unknown"
        keys = ("input", "output", "total", "reasoning", "cache_read", "cache_write")
        normalized_usage = _sanitize_usage(usage)
        normalized_attempts = max(1, _nonnegative_int(attempts, default=1))
        normalized_duration = _nonnegative_finite_float(duration_ms) or 0.0
        self.provider_calls += 1
        self.provider_attempts += normalized_attempts
        self.provider_calls_by_purpose[normalized_purpose] = (
            self.provider_calls_by_purpose.get(normalized_purpose, 0) + 1
        )
        purpose_usage = self.provider_usage_by_purpose.setdefault(
            normalized_purpose,
            {key: 0 for key in keys},
        )
        if not self.provider_usage:
            self.provider_usage = {key: 0 for key in keys}
        for key, value in normalized_usage.items():
            purpose_usage[key] = int(purpose_usage.get(key) or 0) + value
            self.provider_usage[key] = int(self.provider_usage.get(key) or 0) + value
        self.provider_duration_ms_by_purpose[normalized_purpose] = round(
            float(self.provider_duration_ms_by_purpose.get(normalized_purpose) or 0.0)
            + normalized_duration,
            3,
        )
        normalized_provider = str(provider_id or "unknown").strip() or "unknown"
        normalized_model = str(model_id or "unknown").strip() or "unknown"
        model_key = f"{normalized_provider}/{normalized_model}"
        self.provider_calls_by_model[model_key] = (
            self.provider_calls_by_model.get(model_key, 0) + 1
        )
        model_usage = self.provider_usage_by_model.setdefault(
            model_key,
            {key: 0 for key in keys},
        )
        for key, value in normalized_usage.items():
            model_usage[key] = int(model_usage.get(key) or 0) + value
        self.provider_duration_ms_by_model[model_key] = round(
            float(self.provider_duration_ms_by_model.get(model_key) or 0.0)
            + normalized_duration,
            3,
        )
        normalized_cost = None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            candidate = float(cost)
            if math.isfinite(candidate) and candidate >= 0:
                normalized_cost = candidate
        if normalized_cost is None:
            self.provider_cost_unknown_calls += 1
            source = "unknown"
        else:
            self.provider_cost_usd = round(
                self.provider_cost_usd + normalized_cost,
                12,
            )
            self.provider_cost_usd_by_purpose[normalized_purpose] = round(
                self.provider_cost_usd_by_purpose.get(normalized_purpose, 0.0)
                + normalized_cost,
                12,
            )
            self.provider_cost_usd_by_model[model_key] = round(
                self.provider_cost_usd_by_model.get(model_key, 0.0)
                + normalized_cost,
                12,
            )
            source = str(cost_source or "reported").strip() or "reported"
        self.provider_cost_sources[source] = (
            int(self.provider_cost_sources.get(source) or 0) + 1
        )

    def observe_provider_turn(self, record: dict) -> None:
        """Persist one bounded, JSON-safe main-model turn attribution record."""
        normalized = _normalize_provider_turn_record(record)
        if normalized is None:
            return
        reason = normalized["reason"]
        outcome = normalized["outcome"]
        self.provider_turn_records.append(normalized)
        if len(self.provider_turn_records) > PROVIDER_TURN_RECORD_LIMIT:
            self.provider_turn_records = self.provider_turn_records[
                -PROVIDER_TURN_RECORD_LIMIT:
            ]
        self.provider_turns_by_reason[reason] = (
            int(self.provider_turns_by_reason.get(reason) or 0) + 1
        )
        self.provider_turns_by_outcome[outcome] = (
            int(self.provider_turns_by_outcome.get(outcome) or 0) + 1
        )

    def _record_workspace_mutation(self, paths: list[str], operations: dict[str, str] | None = None) -> None:
        """Advance mutation-scoped state for one settled workspace write."""
        self.last_edit_turn = self.turn_count
        self.edits_this_run += 1
        self.mutation_generation += 1
        self.last_mutation_paths = tuple(paths)
        self.has_diff = True
        for path in paths:
            if path not in self.changed_files:
                self.changed_files.append(path)
        if self.acceptance_mutation_generation is None:
            self.acceptance_mutation_generation = max(
                0, self.mutation_generation - 1
            )
        if not paths or not all(is_documentation_file(path) for path in paths):
            self.acceptance_mutation_generation = self.mutation_generation
        if not paths or any(_is_source_mutation_path(path) for path in paths):
            self.source_mutation_generation = self.mutation_generation
        self.investigation_calls_since_edit = 0
        self.strict_progress_nudges = 0
        self.strict_progress_blocks = 0
        self.transition = "edited_source"

    def bind_run_context(self, context) -> None:
        previous = self._verification_context
        if previous is not None and previous is not context and not previous.finalized:
            raise RuntimeError("Previous run settlement is still active")
        self._verification_context = context

    @property
    def verification_domain(self):
        context = self._verification_context
        return context.verification if context is not None else None

    @property
    def verification_contract(self) -> dict:
        definition = self.verification_definition
        command = str(definition.get("command") or "")
        if not command:
            return {}
        targets = tuple(definition.get("targets") or ())
        domain = self.verification_domain
        if domain is not None:
            return domain.contract_projection(command, targets)
        return VerificationContract(command, targets).to_dict()

    @property
    def requirement_ledger(self) -> dict:
        return self.requirement_ledger_snapshot().to_dict() if self.task_contract else {}

    @property
    def changed_files_verified(self) -> bool:
        snapshot, paths = verification_snapshot_with_paths(self)
        return bool(snapshot and snapshot.syntax_coverage(paths)[1])

    @property
    def py_compile_ok(self) -> bool:
        snapshot, paths = verification_snapshot_with_paths(self)
        return bool(snapshot and snapshot.syntax_coverage(paths, python_only=True)[1])

    @property
    def verification_generation(self) -> int:
        domain = self.verification_domain
        snapshot = domain.snapshot() if domain is not None else None
        return snapshot.version.generation if snapshot and snapshot.version.scope_complete and any(
            item.status == "passed" for item in snapshot.checks) else -1

    @property
    def last_verification_failure(self) -> str:
        domain = self.verification_domain
        if domain is None:
            return ""
        failed = [item for item in domain.snapshot().checks if item.status == "failed"]
        return failed[-1].reason if failed else ""

    def to_dict(self, active: bool = True) -> dict:
        """对外投影；恢复只接受 build_runtime_checkpoint 的域快照。"""
        context = self._verification_context
        with context._state_lock if context is not None else nullcontext():
            data = build_runtime_checkpoint(self, active=active)
            data.pop("verification_domain", None)
            data.update(verification=self.verification_domain.snapshot().to_dict() if self.verification_domain is not None else {},
                        verification_contract=self.verification_contract,
                        requirement_ledger=self.requirement_ledger,
                        changed_files_verified=self.changed_files_verified,
                        py_compile_ok=self.py_compile_ok,
                        verification_generation=self.verification_generation,
                        last_verification_failure=self.last_verification_failure)
            return data

    def restore(self, data: dict, *, allow_inactive: bool = False) -> bool:
        """Restore persisted fields, optionally for a resumable activation."""
        if not isinstance(data, dict) or (
            not data.get("active") and not allow_inactive
        ):
            return False
        # Old snapshots have no original reference evidence. Never recapture
        # today's potentially agent-modified file as an original on resume.
        # 显式空值也是已有但损坏的材料，不能与未保存检查点混淆。
        self._restored_verification_domain = (data["verification_domain"] if data["verification_domain"] is not None else {}) if "verification_domain" in data else None
        self.task_reference_evidence = []
        self.execution_handoff = {}
        self.task_authority_epoch = 0
        self.plan_revision = 0
        self.plan_last_commit = {}
        self.task_authority_message_id = ""
        self.task_reference_omitted_count = 0
        self.task_references_bound = True
        # Scratch lifecycle state is deliberately not persisted. Restoring a
        # snapshot must therefore fail closed instead of reusing stale paths
        # from an earlier in-memory activation.
        self._ephemeral_scratch_paths = set()
        persisted_fields = {item.name for item in dataclass_fields(self)}
        for key, value in data.items():
            if key in {"active", "saved_at"}:
                continue
            if key in persisted_fields:
                setattr(self, key, value)
        if "acceptance_mutation_generation" not in data:
            self.acceptance_mutation_generation = _nonnegative_int(
                data.get("mutation_generation")
            )
        self.task_reference_evidence = [ref.to_dict() for ref in
                                        sanitize_references(self.task_reference_evidence)]
        self.task_reference_omitted_count = _nonnegative_int(self.task_reference_omitted_count)
        epoch = self.task_authority_epoch
        self.task_authority_epoch = max(
            epoch if type(epoch) is int and 0 <= epoch <= 2**63 - 1 else 0,
            max((ref["authority_epoch"] for ref in self.task_reference_evidence), default=0),
        )
        identity = self.task_authority_message_id
        self.task_authority_message_id = identity if isinstance(identity, str) and re.fullmatch(r"msg-[A-Za-z0-9_-]{1,128}", identity) else ""
        self.task_references_bound = True
        self._sanitize_restored_control_state()
        from nz_coder.runtime.agent.planning_runtime import sanitize_working_plan_receipt
        sanitize_working_plan_receipt(self)
        self._sanitize_restored_provider_accounting()
        return True

    def _sanitize_restored_control_state(self) -> None:
        """Normalize all non-Provider fields loaded from an untrusted snapshot."""
        nonnegative_defaults = {
            "plan_revision": 0,
            "turn_count": 0,
            "max_turns": 80,
            "timeout_seconds": 0,
            "last_edit_turn": 0,
            "edits_this_run": 0,
            "diff_chars": 0,
            "open_todo_items": 0,
            "completion_gate_prompts": 0,
            "verification_attempts": 0,
            "broad_test_attempts": 0,
            "exact_test_attempts": 0,
            "verification_failures": 0,
            "completion_review_rejections": 0,
            "investigation_calls_since_edit": 0,
            "mutation_generation": 0,
            "source_mutation_generation": 0,
            "strict_progress_nudges": 0,
            "strict_progress_blocks": 0,
            "package_install_attempts": 0,
            "emergency_broad_exploration": 0,
        }
        for name, default in nonnegative_defaults.items():
            setattr(
                self,
                name,
                _nonnegative_int(getattr(self, name), default=default),
            )
        for name in (
            "completion_review_generation",
            "diff_generation",
        ):
            setattr(self, name, _signed_int(getattr(self, name)))

        started_at = _nonnegative_finite_float(self.started_at)
        self.started_at = started_at if started_at is not None else 0.0
        if self.acceptance_mutation_generation is not None:
            self.acceptance_mutation_generation = _nonnegative_int(
                self.acceptance_mutation_generation
            )

        list_limits = {
            "changed_files": 500,
            "acceptance_criteria": 20,
            "requested_paths": 20,
            "supporting_recovery_classifications": 20,
            "recovery_repair_targets": 20,
            "searched_patterns": 30,
            "read_files": 40,
            "budget_zones_emitted": 20,
        }
        for name, limit in list_limits.items():
            setattr(self, name, _string_list(getattr(self, name), limit=limit))

        for name in (
            "task_contract",
            "patch_risk",
            "verification_definition",
        ):
            value = getattr(self, name)
            setattr(self, name, value if isinstance(value, dict) else {})

        from nz_coder.runtime.agent.task_contract import parse_task_contract

        if self.task_contract:
            try:
                self.task_contract = parse_task_contract(
                    self.task_contract
                ).to_dict()
            except (TypeError, ValueError):
                self.task_contract = {}
        if self.verification_definition:
            command = self.verification_definition.get("command")
            targets = self.verification_definition.get("targets")
            self.verification_definition = {"command": command, "targets": _string_list(targets, limit=500)} if isinstance(command, str) else {}
        if self.patch_risk:
            risk_signals = self.patch_risk.get("risk_signals")
            affected_files = self.patch_risk.get("affected_files")
            self.patch_risk["requires_replan"] = (
                self.patch_risk.get("requires_replan")
                if isinstance(self.patch_risk.get("requires_replan"), bool)
                else False
            )
            self.patch_risk["risk_signals"] = [
                item for item in risk_signals[:20]
                if isinstance(item, dict)
            ] if isinstance(risk_signals, list) else []
            self.patch_risk["affected_files"] = _string_list(
                affected_files,
                limit=500,
            )

        raw_scheduled = self.scheduled_verification_generations
        scheduled: dict[str, int] = {}
        if isinstance(raw_scheduled, dict):
            for raw_key, raw_generation in raw_scheduled.items():
                key = str(raw_key or "").strip()
                generation = _signed_int(raw_generation)
                if key and generation >= 0:
                    scheduled[key] = generation
        self.scheduled_verification_generations = scheduled

        for name in (
            "has_diff",
            "tests_modified",
            "source_only",
            "wants_tests",
            "forbids_test_changes",
            "plan_generated",
            "needs_broad_exploration",
            "_state_block_emitted",
            "_diff_seen_from_tool",
        ):
            value = getattr(self, name)
            setattr(self, name, value if isinstance(value, bool) else False)
        if not isinstance(self.workspace_git_available, bool):
            self.workspace_git_available = None

        for name in (
            "plan_text",
            "completion_gate_signature",
            "initial_task_text",
            "current_round_instruction_text",
            "risk_feedback_fingerprint",
            "last_completion_review_rejection",
            "primary_recovery_classification",
            "budget_pressure_zone",
            "work_phase",
            "transition",
        ):
            value = getattr(self, name)
            setattr(self, name, value if isinstance(value, str) else "")

    def _sanitize_restored_provider_accounting(self) -> None:
        """Keep corrupt state files from poisoning future Provider arithmetic."""
        self.provider_calls = _nonnegative_int(self.provider_calls)
        self.provider_attempts = max(
            self.provider_calls,
            _nonnegative_int(self.provider_attempts),
        )
        self.provider_calls_by_purpose = _sanitize_counter_map(
            self.provider_calls_by_purpose
        )
        self.provider_calls_by_model = _sanitize_counter_map(
            self.provider_calls_by_model
        )
        self.provider_usage = _sanitize_usage(self.provider_usage)
        self.provider_usage_by_purpose = _sanitize_nested_usage(
            self.provider_usage_by_purpose
        )
        self.provider_usage_by_model = _sanitize_nested_usage(
            self.provider_usage_by_model
        )
        self.provider_duration_ms_by_purpose = _sanitize_duration_map(
            self.provider_duration_ms_by_purpose
        )
        self.provider_duration_ms_by_model = _sanitize_duration_map(
            self.provider_duration_ms_by_model
        )
        self.provider_cost_usd_by_purpose = _sanitize_cost_map(
            self.provider_cost_usd_by_purpose
        )
        self.provider_cost_usd_by_model = _sanitize_cost_map(
            self.provider_cost_usd_by_model
        )
        persisted_total = _nonnegative_finite_float(self.provider_cost_usd)
        dimension_totals = (
            sum(self.provider_cost_usd_by_purpose.values()),
            sum(self.provider_cost_usd_by_model.values()),
        )
        self.provider_cost_usd = round(
            max(persisted_total or 0.0, *dimension_totals),
            12,
        )
        try:
            unknown_calls = int(self.provider_cost_unknown_calls)
        except (TypeError, ValueError, OverflowError):
            unknown_calls = 0
        self.provider_cost_unknown_calls = max(0, unknown_calls)
        self.provider_cost_sources = _sanitize_counter_map(
            self.provider_cost_sources
        )
        raw_records = (
            self.provider_turn_records
            if isinstance(self.provider_turn_records, list)
            else []
        )
        self.provider_turn_records = [
            normalized
            for record in raw_records[-PROVIDER_TURN_RECORD_LIMIT:]
            if (normalized := _normalize_provider_turn_record(record)) is not None
        ]
        self.provider_turns_by_reason = _sanitize_counter_map(
            self.provider_turns_by_reason
        )
        self.provider_turns_by_outcome = _sanitize_counter_map(
            self.provider_turns_by_outcome
        )

    def begin_resumed_activation(
        self,
        *,
        max_turns: int,
        timeout_seconds: int,
    ) -> None:
        """Rebase per-activation budgets while retaining unfinished task facts."""
        self.turn_count = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.timeout_seconds = timeout_seconds
        self.last_edit_turn = 0
        # 用户开启新一轮执行后重新获得指导，旧任务事实和累计用量继续保留。
        self.completion_gate_prompts = 0
        self.completion_gate_signature = ""
        self.budget_zones_emitted = []
        self.budget_pressure_zone = "green"
        self.work_phase = "normal"
        self._state_block_emitted = False

    def save(self, path: Path, active: bool = True) -> None:
        """Atomically persist a resumable RuntimeState checkpoint."""
        with self._persistence_lock:
            self._write_snapshot(path, build_runtime_checkpoint(self, active=active))

    def commit_execution_handoff(self, value: dict, path: Path | None = None) -> None:
        """写盘在运行锁外；新指令到达后，旧候选不能发布或留在磁盘。"""
        import copy
        from contextlib import nullcontext

        context = self._verification_context
        lock = context._state_lock if context is not None else nullcontext()
        with self._persistence_lock:
            with lock:
                authority = (self._verification_context, self.task_authority_epoch, self.task_authority_message_id)
                candidate = copy.deepcopy(value)
                if "epoch" in candidate and candidate["epoch"] != self.task_authority_epoch:
                    raise ValueError("handoff authority changed before save")
                snapshot = build_runtime_checkpoint(self, active=True)
                snapshot["execution_handoff"] = candidate
            if path is not None:
                self._write_snapshot(path, snapshot)
            with lock:
                changed = authority != (self._verification_context, self.task_authority_epoch, self.task_authority_message_id)
                if changed:
                    current = build_runtime_checkpoint(self, active=True)
                else:
                    self.execution_handoff = candidate
            if changed:
                if path is not None:
                    self._write_snapshot(path, current)
                raise ValueError("handoff authority changed during save")

    @staticmethod
    def _write_snapshot(path: Path, snapshot: dict) -> None:
        write_runtime_snapshot(path, snapshot)

    def load(self, path: Path, *, allow_inactive: bool = False) -> bool:
        """Load active state or an explicitly authorized resumable snapshot."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
            raise CheckpointRestoreError() from error
        if not isinstance(data, dict) or type(data.get("active")) is not bool:
            from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
            raise CheckpointRestoreError()
        return self.restore(data, allow_inactive=allow_inactive)

    # ── Observation ───────────────────────────────────────────────────────────

    def observe_tool(
        self,
        name: str,
        tool_input: dict | None,
        output: str,
        *,
        succeeded: bool | None = None,
        metadata: dict | None = None,
        workspace_changes: dict[str, str] | None = None,
        workspace_scope_incomplete: bool = False,
        verification_version_valid: bool = True,
    ) -> dict | None:
        """根据工具调用更新状态。

        由 AgentLoop 在每次工具执行后调用。
        """
        tool_input = tool_input or {}
        output = output or ""
        acceptance_observation = None

        if self.is_investigation_call(name, tool_input):
            self.investigation_calls_since_edit += 1

        # ── grep_search ──────────────────────────────────────────────────────
        if name == "grep_search":
            pattern = tool_input.get("pattern", "")
            if pattern and pattern not in self.searched_patterns:
                self.searched_patterns.append(pattern)
                if len(self.searched_patterns) > 30:
                    self.searched_patterns = self.searched_patterns[-20:]
            evidence_path = _successful_file_scoped_grep(
                tool_input,
                output,
                succeeded,
            )
            if evidence_path and evidence_path not in self.read_files:
                self.read_files.append(evidence_path)
                if len(self.read_files) > 40:
                    self.read_files = self.read_files[-25:]
            self.transition = "searched"

        # ── exact file reads ──────────────────────────────────────────────────
        elif name in ("read_file", "read_symbol"):
            if succeeded is True:
                self.observe_task_reference_read(metadata)
            evidence_path = _successful_exact_read(
                name,
                tool_input,
                output,
                succeeded,
            )
            if evidence_path and evidence_path not in self.read_files:
                self.read_files.append(evidence_path)
                if len(self.read_files) > 40:
                    self.read_files = self.read_files[-25:]
            self.transition = "read"

        # Repository scopes guide navigation but do not prove exact files read.
        elif name in ("repo_map", "code_references"):
            self.transition = "read"

        # ── todo ─────────────────────────────────────────────────────────────
        elif name == "todo":
            self.open_todo_items = sum(
                1
                for line in output.splitlines()
                if re.match(r"^\[(?: |>)\]\s+", line.strip())
            )

        # ── Task-workspace mutation ──────────────────────────────────────────
        elif is_filesystem_mutation_tool(name):
            if (
                succeeded is not False
                and not output.startswith(("Error:", "Denied"))
                and not bool(tool_input.get("dry_run"))
            ):
                paths = list(collect_filesystem_mutation_paths(tool_input))
                ephemeral, self._ephemeral_scratch_paths = (
                    update_ephemeral_scratch_lifecycle(
                        name,
                        tool_input,
                        output,
                        paths,
                        self._ephemeral_scratch_paths,
                    )
                )
                if ephemeral:
                    self.transition = "edited_scratch"
                else:
                    if workspace_scope_incomplete:
                        # 保留文件工具可确认的产物事实，但不把这些路径冒充完整范围。
                        self._record_workspace_mutation(
                            paths, successful_mutation_operations(name, tool_input, output)
                        )
                    elif workspace_changes is None:
                        self._record_workspace_mutation(
                            paths, successful_mutation_operations(name, tool_input, output)
                        )
                    elif workspace_changes:
                        self._record_workspace_mutation(list(workspace_changes), workspace_changes)
            elif workspace_changes:
                # 执行失败也可能留下部分写入；完整事实优先于成功提示文本。
                self._record_workspace_mutation(list(workspace_changes), workspace_changes)

        # ── diff_status ──────────────────────────────────────────────────────
        elif name == "diff_status":
            self._diff_seen_from_tool = True
            # 解析 diff_status 输出中的关键字段
            if "has_non_empty_diff: true" in output:
                self.has_diff = True
            elif "has_non_empty_diff: false" in output and not self.changed_files:
                self.has_diff = False

            # 提取 diff_chars
            m = re.search(r"diff_chars:\s*(\d+)", output)
            if m:
                self.diff_chars = int(m.group(1))

            # diff 工具的视图可能只覆盖文件工具；不能覆盖已观察到的 shell 变更。
            self.changed_files = list(dict.fromkeys([
                *self.changed_files, *self._parse_changed_files(output),
            ]))

            if "tests_modified: true" in output:
                self.tests_modified = True
            if "source_only: true" in output:
                self.source_only = True
            elif "source_only: false" in output:
                self.source_only = False

            if self.has_diff and self._strict_diff_scope_allowed():
                self.diff_generation = self.mutation_generation
            else:
                self.diff_generation = -1

            self.transition = "checked_diff"

        # 验证进度只是调度计数，结论由执行事实进入域后统一计算。
        elif name in ("verify_changed_files", "verify_project_build"):
            self.verification_attempts += 1
            self.transition = "verified"

        # ── bash ──────────────────────────────────────────────────────────────
        elif name == "bash":
            command = tool_input.get("command", "")
            from nz_coder.tool_platform.command_policy import classify_bash

            shell_classification = classify_bash(str(command or ""))
            if workspace_changes is not None:
                if workspace_changes:
                    self._record_workspace_mutation(list(workspace_changes), workspace_changes)
            elif not workspace_scope_incomplete and shell_classification.get("mutating"):
                self._record_workspace_mutation([])
            if shell_classification.get("reason") in {
                "package install", "package manager write",
            }:
                self.package_install_attempts += 1
            if _is_broad_test_command(command):
                self.verification_attempts += 1
                self.broad_test_attempts += 1
                self.transition = "ran_broad_test"
            elif _is_exact_test(command):
                self.verification_attempts += 1
                self.exact_test_attempts += 1
                self.transition = "ran_exact_test"

        return acceptance_observation

    def _parse_changed_files(self, output: str) -> list[str]:
        """从 diff_status 输出中解析 changed files 列表。

        diff_status 输出格式:
          Changed files:
            path/to/file.py
            ...
          空行或关键词结束文件列表。
        """
        files = []
        in_section = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped in ("Changed files:", "Changed files"):
                in_section = True
                continue
            if not in_section:
                continue
            # 空行结束文件列表
            if not stripped:
                break
            # 元数据行结束文件列表
            if ":" in stripped and not stripped.startswith(("  ", "- ")):
                if any(kw in stripped.lower() for kw in ("has_non_empty", "diff_char",
                        "changed_files", "python_files", "tests_mod", "source_only",
                        "recommendation", "git status")):
                    break
            # 跳过 "(none)"
            if stripped == "(none)":
                continue
            # 文件路径：以空格开头且不含冒号（避免匹配 "has_non_empty_diff: true"）
            if line.startswith("  ") and ":" not in stripped:
                fname = stripped
                if fname and not fname.startswith("has_") and not fname.startswith("diff_"):
                    files.append(fname)
        return files

    # ── State block generation ────────────────────────────────────────────────

    def strict_progress_action(
        self,
        tool_name: str,
        pending: int = 0,
        tool_input: dict | None = None,
    ) -> str:
        """Keep the legacy policy hook advisory-only for API compatibility."""
        return "allow"

    def task_constraint_action(
        self,
        tool_name: str,
        tool_input: dict | None = None,
    ) -> str:
        """Block writes that contradict an explicit immutable-test constraint."""
        if not self.forbids_test_changes:
            return "allow"
        name = str(tool_name or "")
        if not is_filesystem_mutation_tool(name):
            return "allow"
        paths = collect_filesystem_mutation_paths(tool_input or {})
        if not paths:
            return "block"
        return "block" if any(is_test_file(path) for path in paths) else "allow"

    def strict_generation_terminal_ready(self) -> bool:
        """Return whether the current mutation generation has final evidence.

        Diff and verification observations are intentionally order-independent,
        matching InfCodeX's terminal-signal contract: the consumer evaluates the
        settled state rather than the tool that happened to run last.
        """
        generation = self.mutation_generation
        return (
            generation > 0
            and self.diff_generation == generation
            and self.verification_generation == generation
            and self.has_diff
            and self._strict_diff_scope_allowed()
        )

    def verification_contract_ready(self, zone: str) -> bool:
        """Return whether Runtime may run a synthetic acceptance command.

        Open Todo items are deterministic evidence that a budget warning is
        not yet a completion boundary.  Natural completion remains mandatory:
        a model cannot bypass user-declared acceptance merely by stopping early.
        """
        return bool(str(zone or "") == "completion" or self.open_todo_items == 0)

    def closure_phase_action(
        self,
        tool_name: str,
        tool_input: dict | None = None,
    ) -> str:
        """Return the compatibility action from the structured closure decision."""
        return self.closure_phase_decision(tool_name, tool_input)[0]

    def closure_phase_decision(
        self,
        tool_name: str,
        tool_input: dict | None = None,
    ) -> tuple[str, str]:
        """Return a closure action and a stable machine-readable reason."""
        name = str(tool_name or "")
        payload = tool_input or {}
        command = " ".join(str(payload.get("command") or "").split())
        under_pressure = (
            self.work_phase in {"closure_repair", "closure_finalize"}
            or self.budget_pressure_zone in {"orange", "red"}
        )
        if (
            under_pressure
            and self.workspace_git_available is False
            and name == "bash"
            and re.match(r"^git\s+(?:diff|status)(?:\s|$)", command)
        ):
            return "block", "git_required_but_unavailable"
        action = self._closure_phase_action(name, payload)
        if action == "allow":
            return "allow", ""
        return "block", "unsafe_git_compound_command"

    def _closure_phase_action(
        self,
        tool_name: str,
        tool_input: dict | None = None,
    ) -> str:
        """Keep shell mutation safety independent of advisory budget pressure."""
        name = str(tool_name or "")
        payload = tool_input or {}
        if name == "bash":
            command = str(payload.get("command") or "")
            if (
                re.match(r"^git\s+(?:diff|status)(?:\s|$)", command)
                and not _is_read_only_git_observation(command)
            ):
                return "block"
        return "allow"

    def emergency_eligibility(self):
        """Return legacy diagnostic facts; the result never gates execution."""
        from nz_coder.runtime.execution.work_budget import evaluate_emergency_extension

        return evaluate_emergency_extension(
            has_diff=self.has_diff,
            failure_evidence_exists=bool(
                self.verification_failures > 0
                or self.completion_review_rejections > 0
            ),
            repair_target_known=bool(self._known_closure_paths()),
            needs_broad_exploration=self.needs_broad_exploration,
        )

    def _bounded_emergency_action(self, name: str, payload: dict) -> str:
        known_paths = self._known_closure_paths()
        if name in {"read_file", "read_symbol"}:
            path = _closure_path(payload.get("path"))
            return "allow" if path and path in known_paths else "block"
        if is_filesystem_mutation_tool(name):
            paths = {
                _closure_path(path)
                for path in collect_filesystem_mutation_paths(payload)
            }
            return "allow" if paths and paths <= known_paths else "block"
        if name in {"diff_status", "verify_changed_files", "todo", "read_scratchpad"}:
            return "allow"
        if name == "bash":
            from nz_coder.tool_platform.command_policy import classify_bash
            from nz_coder.intelligence.verification_planner import classify_verification_command

            command = str(payload.get("command") or "")
            if (
                self.workspace_git_available is True
                and _is_read_only_git_observation(command)
            ):
                return "allow"
            classification = classify_bash(command)
            if classification.get("reason") in {
                "package install", "package manager write",
            }:
                self.package_install_attempts += 1
                return "block"
            contract_command = str(self.verification_contract.get("command") or "")
            if payload.get("_nz_runtime_contract") or payload.get(
                "_nz_runtime_verification_stage"
            ):
                if self._declared_runtime_verification(payload):
                    return "allow"
                self.emergency_broad_exploration += 1
                return "block"
            if contract_command and " ".join(command.split()) == " ".join(
                contract_command.split()
            ):
                return "allow"
            return (
                "allow"
                if classify_verification_command(command) in {"static", "targeted"}
                else "block"
            )
        if self.is_investigation_call(name, payload) or name in {
            "task", "background_task_start", "background_task_apply",
            "workflow", "list_directory", "glob_search", "repo_map",
            "grep_search", "semantic_search", "code_references",
            "find_symbol_callers", "analyze_impact",
        }:
            self.emergency_broad_exploration += 1
        return "block"

    def _declared_runtime_verification(self, payload: dict) -> bool:
        """Validate internal-looking flags against canonical runtime facts.

        Internal arguments are visible in durable model history, so a model can
        repeat them.  The flag alone is never authority: acceptance must match
        the stored exact command, and staged checks must match the deterministic
        command classifier.
        """
        command = " ".join(str(payload.get("command") or "").split())
        if not command:
            return False
        if payload.get("_nz_runtime_contract"):
            expected = " ".join(
                str(self.verification_contract.get("command") or "").split()
            )
            return bool(expected and command == expected)
        stage = str(payload.get("_nz_runtime_verification_stage") or "").strip()
        if stage not in {"static", "targeted"}:
            return False
        from nz_coder.intelligence.verification_planner import classify_verification_command

        return classify_verification_command(command) == stage

    def _known_closure_paths(self) -> set[str]:
        paths = {
            _closure_path(item)
            for item in (
                list(self.changed_files)
                + list(self.requested_paths)
                + list(self.read_files)
                + list(self.recovery_repair_targets)
            )
            if str(item).strip()
        }
        for requirement in self.task_contract.get("requirements") or []:
            if not isinstance(requirement, dict):
                continue
            for path in requirement.get("expected_artifacts") or []:
                normalized = _closure_path(path)
                if normalized:
                    paths.add(normalized)
        return paths

    def _strict_diff_scope_allowed(self) -> bool:
        """Accept test changes only when the task explicitly calls for them."""
        return bool(
            (self.source_only and not self.tests_modified)
            or (self.tests_modified and self.wants_tests)
        )

    @staticmethod
    def is_investigation_tool(tool_name: str) -> bool:
        """Return whether a tool consumes the strict investigation budget."""
        return str(tool_name or "") in _INVESTIGATION_TOOLS

    @staticmethod
    def is_investigation_call(
        tool_name: str,
        tool_input: dict | None = None,
    ) -> bool:
        """Classify structured and strict read-only Bash investigations."""
        name = str(tool_name or "")
        if name in _INVESTIGATION_TOOLS:
            return True
        if name != "bash":
            return False
        command = str((tool_input or {}).get("command") or "").strip()
        tokens = command.split()
        if not tokens:
            return False
        if tokens[0] in _BASH_INVESTIGATION_COMMANDS:
            return True
        return len(tokens) > 1 and tokens[0] == "git" and tokens[1] == "grep"

    def build_prompt_block(self, strict: bool = False, *, include_current_facts: bool = True) -> str:
        """生成运行观察与预算提示，由请求组装器放入当前动态上下文。

        include_current_facts=False 时，验收事实由独立 renderer 呈现，避免重复展示。
        仅在需要提示时返回非空块；调查提醒的观察计数仍沿用原逻辑。
        本方法不维护模型工作记忆，也不另建任务状态。
        """
        reminders: list[str] = []

        t_now = time.time()
        elapsed = int(t_now - self.started_at) if self.started_at else 0
        turns_remaining = max(0, self.max_turns - self.turn_count)
        time_remaining = max(0, self.timeout_seconds - elapsed) if self.timeout_seconds else 0
        from nz_coder.runtime.verification.verification_contract import declared_acceptance
        checks = declared_acceptance(self)
        acceptance_passed_current_generation = bool(checks) and all(item["status"] == "passed" for item in checks)
        acceptance_is_terminal_evidence = bool(checks) and all(
            item.get("origin") == "model" or item.get("zone") in {"completion", "scheduled"} for item in checks)

        if self.investigation_calls_since_edit >= STRICT_INVESTIGATION_SOFT_LIMIT:
            # 次数是观察，不足以推断调查失败；保持原提醒窗口，不强制编辑或交接。
            reminders.append(
                "INVESTIGATION OBSERVATION: "
                f"{self.investigation_calls_since_edit} investigation calls have completed "
                "since the last source edit."
            )
            if self.strict_progress_nudges == 0:
                reminders.append(
                    "Synthesize the current evidence and choose the useful next step: "
                    "continued investigation, an edit, verification, or explaining a concrete blocker. "
                    "Update the working plan only if the route changes. The call count alone "
                    "does not establish stagnation or require an edit or execution handoff."
                )
                self.strict_progress_nudges += 1

        # ── 1. Turn / Time budget ────────────────────────────────────────────
        if self.has_diff or turns_remaining <= 10 or (time_remaining and time_remaining <= 120):
            budget_lines = [
                f"Turn {self.turn_count}/{self.max_turns}",
                f"edits={self.edits_this_run}, files={len(self.changed_files)}, diff_chars={self.diff_chars}",
            ]
            if self.timeout_seconds:
                budget_lines.append(f"Time {elapsed}s/{self.timeout_seconds}s")
            budget_lines.append(f"{turns_remaining} turns remaining" +
                               (f", {time_remaining}s" if self.timeout_seconds else ""))
            reminders.append(" | ".join(budget_lines))

        # ── 2. Acceptance criteria───────────────────────────────
        if include_current_facts and self.acceptance_criteria and (self.has_diff or self.verification_attempts == 0):
            criteria = "; ".join(self.acceptance_criteria[:5])
            reminders.append(f"Acceptance criteria ({len(self.acceptance_criteria)}): {criteria}")
        if include_current_facts and self.requested_paths and (self.has_diff or self.verification_attempts == 0):
            targets = ", ".join(self.requested_paths[:5])
            reminders.append(
                f"User named target files ({len(self.requested_paths)}): {targets}. Prefer editing those exact paths or the closest existing match; do not create same-basename files elsewhere without confirming."
            )

        if self.patch_risk.get("requires_replan"):
            categories = ", ".join(
                str(item.get("category") or "unknown")
                for item in self.patch_risk.get("risk_signals", [])[:5]
                if isinstance(item, dict)
            ) or "unspecified"
            reminders.append(
                "PATCH RISK OBSERVATION: " + categories + ". Compare the affected declarations with "
                "the current user requirements and evidence; decide whether the approach needs adjustment. "
                "The static signal alone does not establish a defect or require rewriting the plan."
            )

        # ── 3. Diff status（如果有 diff，这是最重要的信息）───────────────────
        if self.has_diff and include_current_facts:
            nc = self.diff_chars
            nf = len(self.changed_files)
            if self.tests_modified and self.wants_tests:
                test_note = " Includes test updates requested by the task; ensure they cover the change."
            elif self.tests_modified:
                test_note = " Includes test files; confirm this matches the user request."
            else:
                test_note = " Task explicitly asks for tests, but no test files have been changed yet." if self.wants_tests else ""
            verify_hint = "Run the narrowest relevant project check before finalizing."
            reminders.append(
                f"DIFF EXISTS: {nc} chars across {nf} file(s).{test_note} {verify_hint}"
            )

        # 测试尝试次数用于观察和恢复，不独立于真实任务预算禁止复测。
        # ── 6. Low budget warning ─────────────────────────────────────────────
        if turns_remaining <= 5 and self.has_diff:
            reminders.append(
                f"CRITICAL: Only {turns_remaining} turns remaining. "
                "Finalize your patch NOW. Do not start new explorations."
            )
        elif turns_remaining <= 10 and self.has_diff:
            reminders.append(
                f"Low budget: {turns_remaining} turns remaining. "
                "Verify and finalize your patch."
            )
        elif (time_remaining and time_remaining <= 60 and self.has_diff):
            reminders.append(
                "CRITICAL: Less than 60s remaining. Finalize your patch NOW."
            )

        # ── 7. Verification passed but still going ────────────────────────────
        if (
            include_current_facts and acceptance_passed_current_generation
            and self.has_diff
            and self.open_todo_items == 0
            and acceptance_is_terminal_evidence
        ):
            reminders.append(
                "DECLARED ACCEPTANCE PASSED for the current mutation generation. "
                "Reconcile remaining requirements and provide a final candidate when ready."
            )
        elif include_current_facts and acceptance_passed_current_generation and self.has_diff:
            if self.open_todo_items:
                detail = (
                    f"but {self.open_todo_items} Todo item(s) remain open. "
                    "Complete those items"
                )
            else:
                detail = (
                    "This was a budget-zone check, not a completion boundary. "
                    "Complete any outstanding requirements"
                )
            reminders.append(
                "INTERMEDIATE ACCEPTANCE PASSED for the current mutation generation. "
                f"{detail}; any later edit will re-arm the declared acceptance command."
            )
        elif include_current_facts and self.py_compile_ok and self.has_diff and self.verification_attempts >= 1:
            if self.turn_count - self.last_edit_turn <= 2:
                reminders.append(
                    "Python compilation passed for the changed files. Check this evidence against "
                    "the remaining requirements; static checks alone do not confirm behavior."
                )

        if not reminders:
            return ""

        block = "<system-reminder>\n" + "\n".join(f"- {r}" for r in reminders) + "\n</system-reminder>"
        return block


def extract_explicit_paths(text: str, limit: int = 5) -> list[str]:
    return explicit_instruction_paths(text, limit=limit)


def extract_explicit_mutation_paths(text: str, limit: int = 5) -> list[str]:
    return list(extract_explicit_mutation_operations(text, limit=limit))



def extract_acceptance_criteria(text: str, limit: int = 5) -> list[str]:
    """从 issue/user 文本中提取轻量验收标准。

    只做保守启发式，不调用模型：优先保留精确测试、FAILED 行、编号/项目符号
    任务项，以及 should/must/中文“应/必须”句子。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    criteria: list[str] = []
    problem_title = re.search(
        r"(?im)^\s*Problem statement:\s*\r?\n\s*([^\r\n]+)",
        text,
    )
    if problem_title:
        _append_unique(criteria, problem_title.group(1).strip()[:180], limit)
    patterns = (
        r"[\w./-]+\.py(?:::[\w.\-]+)+",
        r"FAILED\s+[^\n]+",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text):
            _append_unique(criteria, match.strip(), limit)

    bullet_re = re.compile(r"^(?:\d+[.)]|[-*])\s+(.+)$")
    action_markers = ("新增", "修改", "增加", "更新", "支持", "返回", "抛出", "覆盖", "生成", "编写", "校验", "验证", "add ", "update ", "return ", "raise ", "write ", "create ", "test ")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        match = bullet_re.match(stripped)
        if not match:
            continue
        item = match.group(1).strip()
        lowered = item.lower()
        if any(marker in lowered for marker in ("should", "must", "expected", "failing")):
            _append_unique(criteria, item[:180], limit)
        elif any(marker in item for marker in ("应该", "必须", "期望", "失败", "报错")):
            _append_unique(criteria, item[:180], limit)
        elif any(marker in lowered for marker in action_markers):
            _append_unique(criteria, item[:180], limit)
        elif re.search(r"[\w./-]+\.(?:py|js|ts|go|rs|rb)\b", item, flags=re.IGNORECASE):
            _append_unique(criteria, item[:180], limit)
        if len(criteria) >= limit:
            break

    for sentence in re.split(r"(?<=[.!?。！？])\s+|\n+", text):
        stripped = sentence.strip(" -\t")
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in (" should ", " must ", " expected ", " fails ", " failing ")):
            _append_unique(criteria, stripped[:180], limit)
        elif any(marker in stripped for marker in ("应该", "必须", "期望", "失败", "报错")):
            _append_unique(criteria, stripped[:180], limit)
        if len(criteria) >= limit:
            break
    return criteria


def _append_unique(items: list[str], value: str, limit: int) -> None:
    if value and value not in items and len(items) < limit:
        items.append(value)


def _prioritized_unique(
    current: list[str],
    previous: list[str],
    *,
    limit: int,
) -> list[str]:
    """Keep current-round facts first while retaining bounded prior facts."""
    merged: list[str] = []
    for value in [*current, *previous]:
        _append_unique(merged, value, limit)
    return merged
