"""Immutable execution policy selected once for each top-level Run."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from nz_coder.foundation.run_context import (
    active_run_settings_object,
    bind_run_settings_object,
    reset_run_settings_object,
)
from nz_coder.foundation.secret_values import secret_str
from nz_coder.foundation.workspace_trust import ConfigSnapshot


class _LegacyConfigView:
    """Compatibility input used only when no product Run is active."""

    @staticmethod
    def _value(key: str, default: Any) -> Any:
        from nz_coder.foundation import config

        attribute = key[3:] if key.startswith("NZ_") else key
        return getattr(config, attribute, default)

    def get(self, key: str, default: str = "") -> str:
        value = self._value(key, default)
        return "" if value is None else str(value)

    def get_int(self, key: str, default: int, **_limits) -> int:
        try:
            return int(self._value(key, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, key: str, default: float, **_limits) -> float:
        try:
            return float(self._value(key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_bool(self, key: str, default: bool) -> bool:
        value = self._value(key, default)
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return bool(default)


@dataclass(frozen=True)
class RunSettings:
    """Typed, immutable policy and resource settings for one Run epoch."""

    max_agent_turns: int
    nominal_agent_turns: int
    max_parallel_tasks: int
    provider_max_retries: int
    provider_hard_timeout: float
    provider_stream_idle_timeout: float
    provider_cancel_grace: float
    provider_non_streaming_fallback: bool
    stream_checkpoint_interval: float
    stream_checkpoint_min_chars: int
    stream_delta_interval: float
    stream_delta_min_chars: int
    bash_timeout: int
    bash_output_hard_limit_bytes: int
    allow_package_installs: bool
    max_tool_calls: int
    doom_loop_threshold: int
    read_dedup_enabled: bool
    continue_loop_on_deny: bool
    process_buffer_bytes: int
    process_read_max_bytes: int
    process_write_max_bytes: int
    process_max_per_workspace: int
    process_kill_grace: float
    process_output_encoding: str | None
    write_batch_file_bytes: int
    write_batch_total_bytes: int
    max_context_tokens: int
    max_output_tokens: int
    context_replay_compaction_tokens: int
    system_context_budget_tokens: int
    planning_enabled: bool
    planning_max_tokens: int
    replan_idle_turns: int
    replan_max_attempts: int
    reflection_enabled: bool
    reflection_max_attempts: int
    max_verification_gate_prompts: int
    project_verify_timeout: int
    lsp_enabled: bool
    lsp_initialize_timeout: float
    lsp_request_timeout: float
    lsp_diagnostic_wait: float
    lsp_max_output_chars: int
    lsp_write_diagnostics_enabled: bool
    lsp_write_diagnostic_max_files: int
    mcp_enabled: bool
    mcp_startup_timeout: float
    mcp_tool_timeout: float
    repo_map_max_files: int
    repo_map_max_symbols: int
    repo_map_max_file_bytes: int
    subagent_max_turns: int
    subagent_timeout_seconds: int
    subagent_explore_model: str
    subagent_deep_model: str
    subagent_worktree_enabled: bool
    subagent_background_max_tasks: int
    subagent_background_max_concurrent: int
    subagent_process_isolation_enabled: bool
    subagent_process_stop_grace: float
    memory_llm_rerank: bool
    memory_llm_extract: bool
    memory_async_write: bool
    memory_auto_extract: bool
    memory_auto_dream: bool
    memory_auto_dream_min_hours: int
    memory_auto_dream_min_new_sessions: int
    memory_cleanup_days: int
    runtime_state_persist: bool
    trace_enabled: bool
    image_provider: str
    image_model: str
    image_api_key: str = field(repr=False)
    image_base_url: str
    image_max_tokens: int
    auto_mode_classifier_enabled: bool
    auto_mode_classifier_timeout: float
    auto_mode_classifier_max_output_tokens: int
    auto_mode_classifier_block_streak: int
    auto_mode_classifier_infra_failures: int
    auto_mode_classifier_infra_window: float
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Keep credential text usable while making dataclass projections safe."""
        object.__setattr__(self, "image_api_key", secret_str(self.image_api_key))

    @classmethod
    def from_snapshot(cls, snapshot: ConfigSnapshot) -> "RunSettings":
        return cls._from_view(snapshot, snapshot=snapshot)

    @classmethod
    def from_legacy_globals(cls) -> "RunSettings":
        return cls._from_view(_LegacyConfigView(), snapshot=None)

    @classmethod
    def _from_view(cls, view, *, snapshot) -> "RunSettings":
        integer = view.get_int
        number = view.get_float
        boolean = view.get_bool
        text = view.get
        process_buffer = max(1, integer("NZ_PROCESS_BUFFER_BYTES", 2 * 1024 * 1024))
        max_turns = max(1, integer("MAX_AGENT_TURNS", 500))
        return cls(
            max_agent_turns=max_turns,
            nominal_agent_turns=min(
                max_turns, max(1, integer("NZ_NOMINAL_AGENT_TURNS", 200)),
            ),
            max_parallel_tasks=max(1, integer("MAX_PARALLEL_TASKS", 4)),
            provider_max_retries=max(0, integer("NZ_PROVIDER_MAX_RETRIES", 3)),
            provider_hard_timeout=max(1.0, number("NZ_PROVIDER_HARD_TIMEOUT_SECONDS", 600.0)),
            provider_stream_idle_timeout=max(0.0, number("NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS", 60.0)),
            provider_cancel_grace=min(5.0, max(0.0, number("NZ_PROVIDER_CANCEL_GRACE_SECONDS", 0.25))),
            provider_non_streaming_fallback=boolean("NZ_PROVIDER_NON_STREAMING_FALLBACK", True),
            stream_checkpoint_interval=max(0.05, number("NZ_STREAM_CHECKPOINT_INTERVAL_SECONDS", 0.5)),
            stream_checkpoint_min_chars=max(256, integer("NZ_STREAM_CHECKPOINT_MIN_CHARS", 4_096)),
            stream_delta_interval=min(0.08, max(0.03, number("NZ_STREAM_DELTA_INTERVAL_SECONDS", 0.05))),
            stream_delta_min_chars=max(32, integer("NZ_STREAM_DELTA_MIN_CHARS", 256)),
            bash_timeout=max(1, integer("BASH_TIMEOUT_SECONDS", 120)),
            bash_output_hard_limit_bytes=max(
                process_buffer, integer("NZ_BASH_OUTPUT_HARD_LIMIT_BYTES", 64 * 1024 * 1024),
            ),
            allow_package_installs=boolean("ALLOW_BASH_PACKAGE_INSTALLS", False),
            max_tool_calls=max(1, integer("MAX_TOOL_CALLS_PER_RESPONSE", 20)),
            doom_loop_threshold=max(1, integer("NZ_DOOM_LOOP_THRESHOLD", 3)),
            read_dedup_enabled=boolean("NZ_READ_DEDUP_ENABLED", True),
            continue_loop_on_deny=boolean("NZ_CONTINUE_LOOP_ON_DENY", False),
            process_buffer_bytes=process_buffer,
            process_read_max_bytes=max(1, integer("NZ_PROCESS_READ_MAX_BYTES", 64 * 1024)),
            process_write_max_bytes=max(1, integer("NZ_PROCESS_WRITE_MAX_BYTES", 64 * 1024)),
            process_max_per_workspace=max(1, integer("NZ_PROCESS_MAX_PER_WORKSPACE", 16)),
            process_kill_grace=max(0.0, number("NZ_PROCESS_KILL_GRACE_SECONDS", 0.5)),
            process_output_encoding=(text("NZ_PROCESS_OUTPUT_ENCODING", "").strip() or None),
            write_batch_file_bytes=max(1, integer("NZ_WRITE_BATCH_MAX_FILE_BYTES", 100_000)),
            write_batch_total_bytes=max(1, integer("NZ_WRITE_BATCH_MAX_TOTAL_BYTES", 500_000)),
            max_context_tokens=max(1, integer("MAX_CONTEXT_TOKENS", 100_000)),
            max_output_tokens=max(1, integer("MAX_OUTPUT_TOKENS", 8_000)),
            context_replay_compaction_tokens=max(0, integer("NZ_CONTEXT_REPLAY_COMPACTION_TOKENS", 0)),
            system_context_budget_tokens=max(1, integer("SYSTEM_CONTEXT_BUDGET_TOKENS", 6_000)),
            planning_enabled=boolean("NZ_PLANNING_ENABLED", False),
            planning_max_tokens=max(1, integer("NZ_PLANNING_MAX_TOKENS", 1_500)),
            replan_idle_turns=max(1, integer("NZ_REPLAN_IDLE_TURNS", 5)),
            replan_max_attempts=max(0, integer("NZ_REPLAN_MAX_ATTEMPTS", 2)),
            reflection_enabled=boolean("NZ_REFLECTION_ENABLED", False),
            reflection_max_attempts=max(0, integer("NZ_REFLECTION_MAX_ATTEMPTS", 2)),
            max_verification_gate_prompts=max(0, integer("MAX_VERIFICATION_GATE_PROMPTS", 2)),
            project_verify_timeout=max(1, integer("NZ_PROJECT_VERIFY_TIMEOUT_SECONDS", 60)),
            lsp_enabled=boolean("NZ_LSP_ENABLED", True),
            lsp_initialize_timeout=max(0.001, number("NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", 20.0)),
            lsp_request_timeout=max(0.001, number("NZ_LSP_REQUEST_TIMEOUT_SECONDS", 10.0)),
            lsp_diagnostic_wait=max(0.0, number("NZ_LSP_DIAGNOSTIC_WAIT_SECONDS", 2.0)),
            lsp_max_output_chars=max(1, integer("NZ_LSP_MAX_OUTPUT_CHARS", 20_000)),
            lsp_write_diagnostics_enabled=boolean("NZ_LSP_WRITE_DIAGNOSTICS_ENABLED", True),
            lsp_write_diagnostic_max_files=max(1, integer("NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES", 8)),
            mcp_enabled=boolean("NZ_MCP_ENABLED", False),
            mcp_startup_timeout=max(0.001, number("NZ_MCP_STARTUP_TIMEOUT_SECONDS", 30.0)),
            mcp_tool_timeout=max(0.001, number("NZ_MCP_TOOL_TIMEOUT_SECONDS", 30.0)),
            repo_map_max_files=max(1, integer("NZ_REPO_MAP_MAX_FILES", 80)),
            repo_map_max_symbols=max(1, integer("NZ_REPO_MAP_MAX_SYMBOLS", 600)),
            repo_map_max_file_bytes=max(1, integer("NZ_REPO_MAP_MAX_FILE_BYTES", 1_000_000)),
            subagent_max_turns=max(1, integer("SUBAGENT_MAX_TURNS", 200)),
            subagent_timeout_seconds=max(1, integer("SUBAGENT_TIMEOUT_SECONDS", 180)),
            subagent_explore_model=text("SUBAGENT_EXPLORE_MODEL", ""),
            subagent_deep_model=text("SUBAGENT_DEEP_MODEL", ""),
            subagent_worktree_enabled=boolean("SUBAGENT_WORKTREE_ENABLED", True),
            subagent_background_max_tasks=min(20, max(1, integer("SUBAGENT_BACKGROUND_MAX_TASKS", 20))),
            subagent_background_max_concurrent=max(1, integer("SUBAGENT_BACKGROUND_MAX_CONCURRENT", 4)),
            subagent_process_isolation_enabled=boolean("NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", True),
            subagent_process_stop_grace=max(0.0, number("NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS", 0.5)),
            memory_llm_rerank=boolean("MEMORY_LLM_RERANK", False),
            memory_llm_extract=boolean("MEMORY_LLM_EXTRACT", False),
            memory_async_write=boolean("MEMORY_ASYNC_WRITE", False),
            memory_auto_extract=boolean("MEMORY_AUTO_EXTRACT", True),
            memory_auto_dream=boolean("MEMORY_AUTO_DREAM", True),
            memory_auto_dream_min_hours=max(0, integer("MEMORY_AUTO_DREAM_MIN_HOURS", 24)),
            memory_auto_dream_min_new_sessions=max(0, integer("MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS", 5)),
            memory_cleanup_days=max(0, integer("MEMORY_CLEANUP_DAYS", 30)),
            runtime_state_persist=boolean("RUNTIME_STATE_PERSIST", True),
            trace_enabled=boolean("TRACE_ENABLED", True),
            image_provider=text("NZ_IMAGE_DESCRIBE_PROVIDER", text("MODEL_PROVIDER", "openai-compatible")),
            image_model=text("NZ_IMAGE_DESCRIBE_MODEL", ""),
            image_api_key=text("NZ_IMAGE_DESCRIBE_API_KEY", ""),
            image_base_url=text("NZ_IMAGE_DESCRIBE_BASE_URL", ""),
            image_max_tokens=max(1, integer("NZ_IMAGE_DESCRIBE_MAX_TOKENS", 1_200)),
            auto_mode_classifier_enabled=boolean("NZ_AUTO_MODE_CLASSIFIER_ENABLED", True),
            auto_mode_classifier_timeout=max(1.0, number("NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS", 15.0)),
            auto_mode_classifier_max_output_tokens=max(64, integer("NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS", 256)),
            auto_mode_classifier_block_streak=max(1, integer("NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK", 3)),
            auto_mode_classifier_infra_failures=max(1, integer("NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES", 5)),
            auto_mode_classifier_infra_window=max(1.0, number("NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS", 600.0)),
            snapshot=snapshot,
        )


def active_run_settings() -> RunSettings | None:
    """Return the settings already bound to the current execution context."""
    value = active_run_settings_object()
    return value if isinstance(value, RunSettings) else None


def current_run_settings() -> RunSettings:
    """Return the active immutable settings or an explicit compatibility view."""
    return active_run_settings() or RunSettings.from_legacy_globals()


@contextmanager
def scoped_run_settings(settings: RunSettings) -> Iterator[RunSettings]:
    """Bind exactly one immutable settings epoch to a Run and its child tasks."""
    if not isinstance(settings, RunSettings):
        raise TypeError("settings must be RunSettings")
    token = bind_run_settings_object(settings)
    try:
        yield settings
    finally:
        reset_run_settings_object(token)


__all__ = [
    "RunSettings", "active_run_settings", "current_run_settings",
    "scoped_run_settings",
]
