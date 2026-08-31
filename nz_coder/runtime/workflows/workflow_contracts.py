"""Stable machine-readable contracts for bounded child-Agent workflows."""
from __future__ import annotations

import copy


WORKFLOW_CONTRACT_VERSION = "1.6"

_WORKFLOW_CONTRACT = {
    "version": WORKFLOW_CONTRACT_VERSION,
    "phase_modes": [
        "parallel", "pipeline", "map_reduce", "synthesize",
        "workflow", "quality_gate",
    ],
    "terminal_events": [
        "workflow_run_completed",
        "workflow_run_failed",
        "workflow_run_stopped",
    ],
    "verifier_verdicts": ["accept", "revise", "blocked"],
    "failure_semantics": {
        "ordinary_child_failure": "null-result",
        "structural_failure": "stop-workflow",
        "verifier_transport_failure": "fail-open",
        "abort": "stop-active-children",
    },
    "cache_semantics": {
        "stored_statuses": ["completed", "completed_unverified"],
        "synthesis_replayed": False,
        "resume": "copy-forward",
    },
    "resource_semantics": {
        "agent_cap_includes_synthesis": True,
        "token_budget_checked_before_spawn": True,
        "isolation_modes": ["thread", "process"],
        "nested_workflow_depth": 1,
        "nested_runtime_resources": "shared-with-parent",
    },
    "outcome_semantics": {
        "lineage_entry": "memory_outcome_digest",
        "raw_child_output_persisted": False,
        "idempotency_key": "workflow:<run_id>",
        "run_record": "run.json",
        "artifact_format": "bounded-json",
    },
    "managed_run_semantics": {
        "states": ["running", "paused", "completed", "failed", "stopped"],
        "pause_boundary": "before-agent-spawn",
        "terminal_retention": 500,
        "cross_process_identity": "journal-replay",
        "orphaned_active_run": "fail-closed-on-restart",
        "sdk_first_started": True,
    },
    "capsule_semantics": {
        "format": "nzcoder.workflow",
        "version": 1,
        "executable_source_allowed": False,
        "discovery_precedence": ["project", "personal"],
        "preflight_before_plan_admission": True,
        "archive_recoverable": True,
        "resolution_precedence": ["builtin", "project", "personal"],
        "builtin_names": ["parallel-investigation", "scoped-review"],
    },
    "review_semantics": {
        "diff_capture": "immutable-supplied-bytes",
        "quality_gate": "confirmed-and-unresolved-only",
        "silent_approval": False,
    },
    "worktree_semantics": {
        "terminal_clean_sweep": True,
        "changed_worktree_retained": True,
        "cleanup_failure": "warning",
    },
    "generator_semantics": {
        "format": "json-only-capsule",
        "patterns": [
            "classify-and-act", "fan-out-and-synthesize",
            "adversarial-verification", "generate-and-filter",
            "tournament", "loop-until-done",
        ],
    },
    "host_semantics": {
        "invocation": {
            "command": "suggest",
            "natural-language": "none",
        },
        "turn_consumed_by": ["started", "cancelled"],
        "limit_precedence": "minimum-of-manifest-host-system",
        "identity_kinds": ["run", "saved", "builtin", "ambiguous", "missing"],
        "display_alias_ambiguity": "fail-closed",
        "resume_target": "run-id-or-unique-display-name",
        "authoring": "scout-then-author",
        "approval_binding": "sha256-canonical-effective-summary",
        "stale_approval": "fail-closed",
        "headless_approval": "explicit-auto-receipt",
    },
    "lifecycle_semantics": {
        "run_rename": "display-alias-only",
        "saved_rename": "atomic-exact-scope",
        "saved_delete": "recoverable-trash",
        "saved_replace": "atomic-with-prior-revision",
        "history_projection": "active-plus-persisted-deduplicated",
        "result_summary": "bounded-terminal-record",
        "retention_preview": True,
    },
    "generation_semantics": {
        "envelope": "json-only-decline-or-generate",
        "executable_source_allowed": False,
        "default_timeout_ms": 120000,
        "max_timeout_ms": 600000,
        "repair_attempts": 2,
        "provider_orchestrated": True,
        "single_wall_clock_budget": True,
    },
    "resilience_semantics": {
        "tool_name_repair": "unique-case-separator-equivalence",
        "tool_name_fuzzy_matching": False,
        "tool_result_error_codes": True,
        "retry_description": "classified",
        "terminal_signals": ["COMPLETE", "BLOCKED", "DECIDE"],
        "stream_watchdogs": ["idle", "hard"],
        "non_streaming_fallback": "single-pre-boundary-attempt",
    },
}


def workflow_contract() -> dict:
    """Return a defensive copy suitable for SDKs, traces, and tests."""
    return copy.deepcopy(_WORKFLOW_CONTRACT)
