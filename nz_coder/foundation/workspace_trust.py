"""Typed configuration provenance and user-owned workspace trust records.

Workspace files are parsed as data and never merged into ``os.environ``.  A
security-sensitive workspace value is accepted only when a user-owned trust
record matches the exact workspace identity and current value fingerprint.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import MappingProxyType
from typing import Mapping

from dotenv import dotenv_values

from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.foundation.project_control import (
    ProjectControlSnapshot,
    UnsafeProjectControl,
    capture_project_control_snapshot,
    capture_user_instruction_snapshot,
)
from nz_coder.foundation.languages import LSP_LANGUAGES, lsp_command_config_key


_SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 1024 * 1024
_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class ConfigSpec:
    """Schema entry for one product-owned configuration key.

    Workspace values are privileged by default.  A newly added setting must
    therefore be explicitly downgraded to a non-governing workspace hint.
    """

    default: str | None = None
    secret: bool = False
    workspace_trust_required: bool = True
    value_type: str = "string"
    minimum: float | None = None
    maximum: float | None = None
    public_projection: str = "value"
    opaque_configured: bool = False
    structured_redaction: bool = False
    allowed_sources: tuple[str, ...] = (
        "environment", "user", "workspace", "trusted-workspace",
    )


_KNOWN_CONFIG_KEYS = (
    "ALLOW_BASH_PACKAGE_INSTALLS", "ANTHROPIC_API_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_VERSION", "API_BASE_URL", "API_KEY",
    "BASH_TIMEOUT_SECONDS", "GEMINI_API_BASE_URL", "GEMINI_API_KEY", "LOG_LEVEL",
    "MAX_AGENT_TURNS", "MAX_CONTEXT_TOKENS", "MAX_OUTPUT_TOKENS",
    "MAX_PARALLEL_TASKS", "MAX_TOOL_CALLS_PER_RESPONSE",
    "MAX_VERIFICATION_GATE_PROMPTS", "MEMORY_ASYNC_WRITE", "MEMORY_AUTO_DREAM",
    "MEMORY_AUTO_DREAM_MIN_HOURS", "MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS",
    "MEMORY_AUTO_EXTRACT", "MEMORY_CLEANUP_DAYS", "MEMORY_LLM_EXTRACT",
    "MEMORY_LLM_RERANK", "MODEL_CAPABILITIES_JSON", "MODEL_CATALOG_JSON",
    "MODEL_CATALOG_PATH", "MODEL_ID", "MODEL_PROVIDER", "MODEL_VARIANT",
    "NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK", "NZ_AUTO_MODE_CLASSIFIER_ENABLED",
    "NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES",
    "NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS",
    "NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS",
    "NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS", "NZ_BASH_OUTPUT_HARD_LIMIT_BYTES",
    "NZ_CONTEXT_REPLAY_COMPACTION_TOKENS", "NZ_CONTINUE_LOOP_ON_DENY",
    "NZ_DOOM_LOOP_THRESHOLD", "NZ_IMAGE_DESCRIBE_API_KEY",
    "NZ_IMAGE_DESCRIBE_BASE_URL", "NZ_IMAGE_DESCRIBE_MAX_TOKENS",
    "NZ_IMAGE_DESCRIBE_MODEL", "NZ_IMAGE_DESCRIBE_PROVIDER",
    "NZ_LSP_DIAGNOSTIC_WAIT_SECONDS", "NZ_LSP_ENABLED",
    "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", "NZ_LSP_MAX_OUTPUT_CHARS",
    "NZ_LSP_PYTHON_COMMAND", "NZ_LSP_REQUEST_TIMEOUT_SECONDS",
    "NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES", "NZ_LSP_WRITE_DIAGNOSTICS_ENABLED",
    "NZ_MCP_ENABLED", "NZ_MCP_PROJECT_CONFIG", "NZ_MCP_SERVERS_JSON",
    "NZ_MCP_STARTUP_TIMEOUT_SECONDS", "NZ_MCP_TOOL_TIMEOUT_SECONDS",
    "NZ_MCP_TRUST_STORE", "NZ_MCP_USER_CONFIG", "NZ_MODEL_REGISTRY_PATH",
    "NZ_MODEL_REGISTRY_TTL_SECONDS", "NZ_MODEL_REGISTRY_URL",
    "NZ_NOMINAL_AGENT_TURNS", "NZ_PLANNING_ENABLED", "NZ_PLANNING_MAX_TOKENS",
    "NZ_PROCESS_BUFFER_BYTES", "NZ_PROCESS_KILL_GRACE_SECONDS",
    "NZ_PROCESS_MAX_PER_WORKSPACE", "NZ_PROCESS_OUTPUT_ENCODING",
    "NZ_PROCESS_READ_MAX_BYTES", "NZ_PROCESS_WRITE_MAX_BYTES",
    "NZ_PROJECT_VERIFY_TIMEOUT_SECONDS", "NZ_PROVIDER_CANCEL_GRACE_SECONDS",
    "NZ_PROVIDER_HARD_TIMEOUT_SECONDS", "NZ_PROVIDER_MAX_RETRIES",
    "NZ_PROVIDER_NON_STREAMING_FALLBACK", "NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS",
    "NZ_READ_DEDUP_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_REFLECTION_MAX_ATTEMPTS",
    "NZ_REMOTE_EVENT_QUEUE_SIZE", "NZ_REPLAN_IDLE_TURNS", "NZ_REPLAN_MAX_ATTEMPTS",
    "NZ_REPO_MAP_MAX_FILE_BYTES", "NZ_REPO_MAP_MAX_FILES", "NZ_REPO_MAP_MAX_SYMBOLS",
    "NZ_REPO_RETRIEVAL_STRATEGY", "NZ_SEMANTIC_MODEL",
    "NZ_STREAM_CHECKPOINT_INTERVAL_SECONDS", "NZ_STREAM_CHECKPOINT_MIN_CHARS",
    "NZ_STREAM_DELTA_INTERVAL_SECONDS", "NZ_STREAM_DELTA_MIN_CHARS",
    "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", "NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS",
    "NZ_SWE_NOMINAL_AGENT_TURNS", "NZ_WRITE_BATCH_MAX_FILE_BYTES",
    "NZ_WRITE_BATCH_MAX_TOTAL_BYTES", "OPENAI_API_BASE_URL", "OPENAI_API_KEY",
    "PERMISSION_MODE", "RUNTIME_STATE_PERSIST", "SUBAGENT_BACKGROUND_MAX_CONCURRENT",
    "SUBAGENT_BACKGROUND_MAX_TASKS", "SUBAGENT_DEEP_MODEL", "SUBAGENT_EXPLORE_MODEL",
    "SUBAGENT_MAX_TURNS", "SUBAGENT_TIMEOUT_SECONDS", "SUBAGENT_WORKTREE_ENABLED",
    "SYSTEM_CONTEXT_BUDGET_TOKENS", "TRACE_ENABLED",
)

CONFIG_SCHEMA: dict[str, ConfigSpec] = {
    key: ConfigSpec() for key in _KNOWN_CONFIG_KEYS
}
CONFIG_SCHEMA.update({
    "API_KEY": ConfigSpec("", secret=True),
    "OPENAI_API_KEY": ConfigSpec("", secret=True),
    "ANTHROPIC_API_KEY": ConfigSpec("", secret=True),
    "GEMINI_API_KEY": ConfigSpec("", secret=True),
    "NZ_IMAGE_DESCRIBE_API_KEY": ConfigSpec("", secret=True),
    "MODEL_PROVIDER": ConfigSpec("openai-compatible"),
    "MODEL_ID": ConfigSpec("deepseek-v4-flash"),
    "MODEL_VARIANT": ConfigSpec(""),
    "API_BASE_URL": ConfigSpec("https://api.deepseek.com"),
    "OPENAI_API_BASE_URL": ConfigSpec("https://api.openai.com/v1"),
    "ANTHROPIC_API_BASE_URL": ConfigSpec("https://api.anthropic.com"),
    "GEMINI_API_BASE_URL": ConfigSpec(
        "https://generativelanguage.googleapis.com/v1beta"
    ),
    "PERMISSION_MODE": ConfigSpec("default"),
    "NZ_MCP_ENABLED": ConfigSpec("0", value_type="bool"),
    "NZ_MCP_SERVERS_JSON": ConfigSpec(
        "", public_projection="configured-status",
        opaque_configured=True, structured_redaction=True,
    ),
    "NZ_LSP_PYTHON_COMMAND": ConfigSpec(""),
    "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED": ConfigSpec("1", value_type="bool"),
    "LOG_LEVEL": ConfigSpec("INFO", workspace_trust_required=False),
})
for _structured_key in ("MODEL_CAPABILITIES_JSON", "MODEL_CATALOG_JSON"):
    CONFIG_SCHEMA[_structured_key] = ConfigSpec(
        "", public_projection="configured-status",
        opaque_configured=True, structured_redaction=True,
    )
for _language in LSP_LANGUAGES:
    CONFIG_SCHEMA[lsp_command_config_key(_language)] = ConfigSpec("")

_BOOL_CONFIG_KEYS = (
    "ALLOW_BASH_PACKAGE_INSTALLS", "MEMORY_ASYNC_WRITE", "MEMORY_AUTO_DREAM",
    "MEMORY_AUTO_EXTRACT", "MEMORY_LLM_EXTRACT", "MEMORY_LLM_RERANK",
    "NZ_AUTO_MODE_CLASSIFIER_ENABLED", "NZ_CONTINUE_LOOP_ON_DENY", "NZ_LSP_ENABLED",
    "NZ_LSP_WRITE_DIAGNOSTICS_ENABLED", "NZ_PLANNING_ENABLED",
    "NZ_PROVIDER_NON_STREAMING_FALLBACK", "NZ_READ_DEDUP_ENABLED",
    "NZ_REFLECTION_ENABLED", "RUNTIME_STATE_PERSIST", "SUBAGENT_WORKTREE_ENABLED",
    "TRACE_ENABLED", "NZ_MCP_ENABLED", "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED",
)
for _key in _BOOL_CONFIG_KEYS:
    CONFIG_SCHEMA[_key] = ConfigSpec(value_type="bool")

_INTEGER_CONFIG_KEYS = (
    "BASH_TIMEOUT_SECONDS", "MAX_AGENT_TURNS", "MAX_CONTEXT_TOKENS",
    "MAX_OUTPUT_TOKENS", "MAX_PARALLEL_TASKS", "MAX_TOOL_CALLS_PER_RESPONSE",
    "MAX_VERIFICATION_GATE_PROMPTS", "MEMORY_AUTO_DREAM_MIN_HOURS",
    "MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS", "MEMORY_CLEANUP_DAYS",
    "NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK", "NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES",
    "NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS", "NZ_BASH_OUTPUT_HARD_LIMIT_BYTES",
    "NZ_CONTEXT_REPLAY_COMPACTION_TOKENS", "NZ_DOOM_LOOP_THRESHOLD",
    "NZ_IMAGE_DESCRIBE_MAX_TOKENS", "NZ_LSP_MAX_OUTPUT_CHARS",
    "NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES", "NZ_MODEL_REGISTRY_TTL_SECONDS",
    "NZ_NOMINAL_AGENT_TURNS", "NZ_PLANNING_MAX_TOKENS", "NZ_PROCESS_BUFFER_BYTES",
    "NZ_PROCESS_MAX_PER_WORKSPACE", "NZ_PROCESS_READ_MAX_BYTES",
    "NZ_PROCESS_WRITE_MAX_BYTES", "NZ_PROJECT_VERIFY_TIMEOUT_SECONDS",
    "NZ_PROVIDER_MAX_RETRIES", "NZ_REFLECTION_MAX_ATTEMPTS", "NZ_REMOTE_EVENT_QUEUE_SIZE",
    "NZ_REPLAN_IDLE_TURNS", "NZ_REPLAN_MAX_ATTEMPTS", "NZ_REPO_MAP_MAX_FILE_BYTES",
    "NZ_REPO_MAP_MAX_FILES", "NZ_REPO_MAP_MAX_SYMBOLS", "NZ_STREAM_CHECKPOINT_MIN_CHARS",
    "NZ_SWE_NOMINAL_AGENT_TURNS", "NZ_WRITE_BATCH_MAX_FILE_BYTES",
    "NZ_WRITE_BATCH_MAX_TOTAL_BYTES", "SUBAGENT_BACKGROUND_MAX_CONCURRENT",
    "SUBAGENT_BACKGROUND_MAX_TASKS", "SUBAGENT_MAX_TURNS", "SUBAGENT_TIMEOUT_SECONDS",
    "SYSTEM_CONTEXT_BUDGET_TOKENS",
)
for _key in _INTEGER_CONFIG_KEYS:
    CONFIG_SCHEMA[_key] = ConfigSpec(value_type="int", minimum=0)

_FLOAT_CONFIG_KEYS = (
    "NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS",
    "NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS", "NZ_LSP_DIAGNOSTIC_WAIT_SECONDS",
    "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", "NZ_LSP_REQUEST_TIMEOUT_SECONDS",
    "NZ_MCP_STARTUP_TIMEOUT_SECONDS", "NZ_MCP_TOOL_TIMEOUT_SECONDS",
    "NZ_PROCESS_KILL_GRACE_SECONDS", "NZ_PROVIDER_CANCEL_GRACE_SECONDS",
    "NZ_PROVIDER_HARD_TIMEOUT_SECONDS", "NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS",
    "NZ_STREAM_CHECKPOINT_INTERVAL_SECONDS", "NZ_STREAM_DELTA_INTERVAL_SECONDS",
    "NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS",
)
for _key in _FLOAT_CONFIG_KEYS:
    CONFIG_SCHEMA[_key] = ConfigSpec(value_type="float", minimum=0)

_TYPED_DEFAULTS = {
    "ALLOW_BASH_PACKAGE_INSTALLS": "0",
    "MEMORY_ASYNC_WRITE": "0", "MEMORY_AUTO_DREAM": "1",
    "MEMORY_AUTO_EXTRACT": "1", "MEMORY_LLM_EXTRACT": "0",
    "MEMORY_LLM_RERANK": "0", "NZ_AUTO_MODE_CLASSIFIER_ENABLED": "1",
    "NZ_CONTINUE_LOOP_ON_DENY": "0", "NZ_LSP_ENABLED": "1",
    "NZ_LSP_WRITE_DIAGNOSTICS_ENABLED": "1", "NZ_MCP_ENABLED": "0",
    "NZ_PLANNING_ENABLED": "0", "NZ_PROVIDER_NON_STREAMING_FALLBACK": "1",
    "NZ_READ_DEDUP_ENABLED": "1", "NZ_REFLECTION_ENABLED": "0",
    "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED": "1", "RUNTIME_STATE_PERSIST": "1",
    "SUBAGENT_WORKTREE_ENABLED": "1", "TRACE_ENABLED": "1",
    "BASH_TIMEOUT_SECONDS": "120", "MAX_AGENT_TURNS": "500",
    "MAX_CONTEXT_TOKENS": "100000", "MAX_OUTPUT_TOKENS": "8000",
    "MAX_PARALLEL_TASKS": "4", "MAX_TOOL_CALLS_PER_RESPONSE": "20",
    "MAX_VERIFICATION_GATE_PROMPTS": "2", "MEMORY_AUTO_DREAM_MIN_HOURS": "24",
    "MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS": "5", "MEMORY_CLEANUP_DAYS": "30",
    "NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK": "3",
    "NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES": "5",
    "NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS": "256",
    "NZ_BASH_OUTPUT_HARD_LIMIT_BYTES": str(64 * 1024 * 1024),
    "NZ_CONTEXT_REPLAY_COMPACTION_TOKENS": "0", "NZ_DOOM_LOOP_THRESHOLD": "3",
    "NZ_IMAGE_DESCRIBE_MAX_TOKENS": "1200", "NZ_LSP_MAX_OUTPUT_CHARS": "20000",
    "NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES": "8", "NZ_MODEL_REGISTRY_TTL_SECONDS": "300",
    "NZ_NOMINAL_AGENT_TURNS": "200", "NZ_PLANNING_MAX_TOKENS": "1500",
    "NZ_PROCESS_BUFFER_BYTES": str(2 * 1024 * 1024),
    "NZ_PROCESS_MAX_PER_WORKSPACE": "16", "NZ_PROCESS_READ_MAX_BYTES": str(64 * 1024),
    "NZ_PROCESS_WRITE_MAX_BYTES": str(64 * 1024), "NZ_PROJECT_VERIFY_TIMEOUT_SECONDS": "60",
    "NZ_PROVIDER_MAX_RETRIES": "3", "NZ_REFLECTION_MAX_ATTEMPTS": "2",
    "NZ_REMOTE_EVENT_QUEUE_SIZE": "512", "NZ_REPLAN_IDLE_TURNS": "5",
    "NZ_REPLAN_MAX_ATTEMPTS": "2", "NZ_REPO_MAP_MAX_FILE_BYTES": "1000000",
    "NZ_REPO_MAP_MAX_FILES": "80", "NZ_REPO_MAP_MAX_SYMBOLS": "600",
    "NZ_STREAM_CHECKPOINT_MIN_CHARS": "4096", "NZ_SWE_NOMINAL_AGENT_TURNS": "200",
    "NZ_WRITE_BATCH_MAX_FILE_BYTES": "100000", "NZ_WRITE_BATCH_MAX_TOTAL_BYTES": "500000",
    "SUBAGENT_BACKGROUND_MAX_CONCURRENT": "4", "SUBAGENT_BACKGROUND_MAX_TASKS": "20",
    "SUBAGENT_MAX_TURNS": "200", "SUBAGENT_TIMEOUT_SECONDS": "180",
    "SYSTEM_CONTEXT_BUDGET_TOKENS": "6000",
    "NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS": "600",
    "NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS": "15",
    "NZ_LSP_DIAGNOSTIC_WAIT_SECONDS": "2", "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS": "20",
    "NZ_LSP_REQUEST_TIMEOUT_SECONDS": "10", "NZ_MCP_STARTUP_TIMEOUT_SECONDS": "30",
    "NZ_MCP_TOOL_TIMEOUT_SECONDS": "30", "NZ_PROCESS_KILL_GRACE_SECONDS": "0.5",
    "NZ_PROVIDER_CANCEL_GRACE_SECONDS": "0.25", "NZ_PROVIDER_HARD_TIMEOUT_SECONDS": "600",
    "NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS": "60",
    "NZ_STREAM_CHECKPOINT_INTERVAL_SECONDS": "0.5", "NZ_STREAM_DELTA_INTERVAL_SECONDS": "0.05",
    "NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS": "0.5",
}
for _key, _default in _TYPED_DEFAULTS.items():
    _spec = CONFIG_SCHEMA[_key]
    CONFIG_SCHEMA[_key] = replace(_spec, default=_default)

DEFAULT_CONFIG_VALUES: dict[str, str] = {
    key: spec.default for key, spec in CONFIG_SCHEMA.items()
    if spec.default is not None
}
_SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "COOKIE")
_EXECUTION_MARKERS = (
    "AUTO_EXEC",
    "AUTO_RUN",
    "ALLOW_NETWORK",
    "ALLOW_SHELL",
    "DISABLE_SECURITY",
    "SKIP_PERMISSION",
)


class ConfigSource(str, Enum):
    """Origin of one effective configuration value."""

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    USER = "user"
    WORKSPACE = "workspace"
    TRUSTED_WORKSPACE = "trusted-workspace"


@dataclass(frozen=True)
class ConfigValue:
    """One effective value and the security metadata used to select it."""

    key: str
    value: str
    source: ConfigSource
    ignored: bool = False
    requires_trust: bool = False
    used_default: bool = False
    secret: bool = False

    def public(self, spec: ConfigSpec | None = None) -> dict[str, object]:
        opaque = bool(spec and spec.opaque_configured)
        return {
            "value": (
                "<configured>" if bool(self.value) else "<not-configured>"
            ) if self.secret or opaque else self.value,
            "source": self.source.value,
            "ignored": self.ignored,
            "requires_trust": self.requires_trust,
            "used_default": self.used_default,
        }


@dataclass(frozen=True)
class ConfigIssue:
    """A bounded validation issue that is safe for operator-facing output."""

    key: str
    message: str
    source: ConfigSource


class ConfigValidationError(ValueError):
    """Raised when a configuration or trust document is structurally unsafe."""


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable effective configuration selected at one capture boundary."""

    workspace: Path
    workspace_fingerprint: str
    workspace_trusted: bool
    control_fingerprint: str
    control_plane_trusted: bool
    project_control: ProjectControlSnapshot = field(repr=False)
    values: Mapping[str, ConfigValue] = field(repr=False)
    issues: tuple[ConfigIssue, ...] = field(default_factory=tuple, repr=False)
    user_instructions: ProjectControlSnapshot | None = field(
        default=None, repr=False,
    )

    def __reduce__(self):  # noqa: ANN204
        """Preserve immutable mappings across private spawn IPC boundaries."""
        return (
            _restore_config_snapshot,
            (
                self.workspace,
                self.workspace_fingerprint,
                self.workspace_trusted,
                self.control_fingerprint,
                self.control_plane_trusted,
                self.project_control,
                dict(self.values),
                tuple(self.issues),
                self.user_instructions,
            ),
        )

    def get(self, key: str, default: str | None = None) -> str:
        record = self.values.get(key)
        if record is not None:
            return record.value
        fallback = "" if default is None else str(default)
        spec = CONFIG_SCHEMA.get(str(key))
        return (
            str(spec.default)
            if spec is not None and spec.default is not None
            else fallback
        )

    def value(self, key: str) -> ConfigValue:
        record = self.values.get(key)
        if record is not None:
            return record
        spec = CONFIG_SCHEMA.get(str(key), ConfigSpec())
        return ConfigValue(
            key,
            "" if spec.default is None else str(spec.default),
            ConfigSource.DEFAULT,
            used_default=True,
            secret=spec.secret,
        )

    def get_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw = self.get(key, str(default))
        try:
            value = int(raw)
            if minimum is not None and value < minimum:
                raise ValueError(f"must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"must be <= {maximum}")
            return value
        except (TypeError, ValueError):
            return default

    def get_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        raw = self.get(key, str(default))
        try:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("must be finite")
            if minimum is not None and value < minimum:
                raise ValueError(f"must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"must be <= {maximum}")
            return value
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        raw = self.get(key, "1" if default else "0").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default

    def public(self) -> dict[str, dict[str, object]]:
        return {
            key: value.public(CONFIG_SCHEMA.get(key))
            for key, value in sorted(self.values.items())
        }

    def public_json(self) -> str:
        return json.dumps(self.public(), ensure_ascii=False, sort_keys=True)


def _restore_config_snapshot(
    workspace: Path,
    workspace_fingerprint: str,
    workspace_trusted: bool,
    control_fingerprint: str,
    control_plane_trusted: bool,
    project_control: ProjectControlSnapshot,
    values: dict[str, ConfigValue],
    issues: tuple[ConfigIssue, ...],
    user_instructions: ProjectControlSnapshot | None,
) -> ConfigSnapshot:
    """Rebuild a frozen ConfigSnapshot after private multiprocessing transport."""
    return ConfigSnapshot(
        workspace=Path(workspace),
        workspace_fingerprint=str(workspace_fingerprint),
        workspace_trusted=bool(workspace_trusted),
        control_fingerprint=str(control_fingerprint),
        control_plane_trusted=bool(control_plane_trusted),
        project_control=project_control,
        values=MappingProxyType(dict(values)),
        issues=tuple(issues),
        user_instructions=user_instructions,
    )

_ACTIVE_CONFIG_SNAPSHOT: ContextVar[ConfigSnapshot | None] = ContextVar(
    "nz_coder_active_config_snapshot",
    default=None,
)


@contextmanager
def scoped_config_snapshot(snapshot: ConfigSnapshot):
    """Bind one immutable-at-run-boundary configuration selection."""
    token = _ACTIVE_CONFIG_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _ACTIVE_CONFIG_SNAPSHOT.reset(token)


def current_config_snapshot(workspace: Path | None = None) -> ConfigSnapshot:
    """Return the matching run snapshot or capture the requested workspace."""
    snapshot = active_config_snapshot(workspace)
    if snapshot is not None:
        return snapshot
    if workspace is None:
        raise RuntimeError("No run-scoped ConfigSnapshot is active")
    return load_config_snapshot(Path(workspace))


def active_config_snapshot(workspace: Path | None = None) -> ConfigSnapshot | None:
    """Return only an already-bound matching snapshot, without disk access."""
    snapshot = _ACTIVE_CONFIG_SNAPSHOT.get()
    if snapshot is None or workspace is None:
        return snapshot
    requested = Path(workspace).expanduser().absolute()
    if os.path.normcase(os.path.normpath(str(requested))) == os.path.normcase(
        os.path.normpath(str(snapshot.workspace))
    ):
        return snapshot
    return None


def inherited_config_snapshot(
    snapshot: ConfigSnapshot,
    workspace: Path | str,
) -> ConfigSnapshot:
    """Rebind one parent epoch to its private child worktree without recapture.

    Values and captured control bytes remain those approved for the parent Run;
    only workspace-relative execution is moved to the owned child directory.
    """
    target = Path(workspace).expanduser().absolute()
    if target == snapshot.workspace:
        return snapshot
    return replace(
        snapshot,
        workspace=target,
    )


def default_user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-owned provider/configuration file outside workspaces."""
    environment = os.environ if environ is None else environ
    configured = str(environment.get("NZ_CODER_USER_CONFIG", "")).strip()
    if configured:
        return Path(configured).expanduser().absolute()
    if os.name == "nt" and environment.get("APPDATA"):
        return Path(environment["APPDATA"]) / "nz-coder" / "config.env"
    base = Path(environment.get("XDG_CONFIG_HOME", "")).expanduser() if environment.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "nz-coder" / "config.env"


def default_trust_store_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = str(environment.get("NZ_CODER_WORKSPACE_TRUST_STORE", "")).strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return default_user_config_path(environment).with_name("workspace-trust.json")


def is_secret_config_key(key: str) -> bool:
    upper = str(key).upper()
    spec = CONFIG_SCHEMA.get(upper)
    return spec.secret if spec is not None else any(
        marker in upper for marker in _SECRET_MARKERS
    )


def is_sensitive_config_key(key: str) -> bool:
    upper = str(key).upper()
    spec = CONFIG_SCHEMA.get(upper)
    if spec is not None:
        return spec.workspace_trust_required
    return True


def workspace_config_fingerprint(values: Mapping[str, str]) -> str:
    """Fingerprint only security-relevant workspace assignments."""
    sensitive = {
        str(key): str(value)
        for key, value in values.items()
        if (
            str(key) in CONFIG_SCHEMA
            and CONFIG_SCHEMA[str(key)].workspace_trust_required
        )
    }
    payload = json.dumps(sensitive, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def workspace_control_fingerprint(
    workspace: Path,
    workspace_values: Mapping[str, str] | None = None,
) -> str:
    """Bind execution authority to every repository-owned control input."""
    root = Path(workspace).expanduser().absolute()
    values = workspace_values
    if values is None:
        values, _issues = _read_env_file(root / ".env", ConfigSource.WORKSPACE)
    try:
        snapshot = capture_project_control_snapshot(
            root,
            workspace_config_fingerprint=workspace_config_fingerprint(values),
        )
    except UnsafeProjectControl as exc:
        raise ConfigValidationError(str(exc)) from exc
    return snapshot.fingerprint


class WorkspaceTrustStore:
    """Private, schema-versioned trust records bound to exact workspaces."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_trust_store_path()).expanduser().absolute()

    def is_trusted(
        self,
        workspace: Path,
        config_type: str,
        fingerprint: str,
        *,
        executable: str = "",
    ) -> bool:
        if not fingerprint:
            return False
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        record = self._read().get(key)
        return bool(
            isinstance(record, dict)
            and record.get("workspace") == identity
            and record.get("config_type") == config_type
            and record.get("fingerprint") == fingerprint
            and record.get("executable") == executable
        )

    def trust(
        self,
        workspace: Path,
        config_type: str,
        fingerprint: str,
        *,
        executable: str = "",
    ) -> None:
        if not fingerprint:
            raise ValueError("Trust fingerprint cannot be empty")
        _ensure_store_outside_workspace(self.path, workspace)
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        with self._exclusive_lock():
            entries = self._read_unlocked()
            entries[key] = {
                "workspace": identity,
                "config_type": str(config_type),
                "fingerprint": str(fingerprint),
                "executable": str(executable),
            }
            self._write_unlocked(entries)

    def remove(self, workspace: Path, config_type: str, *, executable: str = "") -> bool:
        identity = _workspace_identity(workspace)
        key = _trust_key(identity, config_type, executable)
        with self._exclusive_lock():
            entries = self._read_unlocked()
            removed = entries.pop(key, None) is not None
            if removed:
                self._write_unlocked(entries)
            return removed

    def _read(self) -> dict[str, dict[str, object]]:
        with self._exclusive_lock():
            return self._read_unlocked()

    @contextmanager
    def _exclusive_lock(self):
        with _STORE_LOCK:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            harden_private_path(self.path.parent)
            with exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
                yield

    def _read_unlocked(self) -> dict[str, dict[str, object]]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigValidationError("Workspace trust store must be a regular file")
        if info.st_size > _MAX_CONFIG_BYTES:
            raise ConfigValidationError("Workspace trust store exceeds 1 MiB")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigValidationError("Workspace trust store is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            raise ConfigValidationError("Workspace trust store schema is invalid")
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise ConfigValidationError("Workspace trust store entries are invalid")
        return {str(key): value for key, value in entries.items() if isinstance(value, dict)}

    def _write_unlocked(self, entries: Mapping[str, object]) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        harden_private_path(parent)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
        temporary = Path(name)
        try:
            payload = json.dumps(
                {"version": _SCHEMA_VERSION, "entries": dict(sorted(entries.items()))},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            harden_private_path(self.path)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)


def load_config_snapshot(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    user_config_path: Path | None = None,
    trust_store: WorkspaceTrustStore | None = None,
) -> ConfigSnapshot:
    """Load shell, user, and workspace configuration without side effects."""
    root = Path(workspace).expanduser().absolute()
    environment = dict(os.environ if environ is None else environ)
    user_path = Path(user_config_path or default_user_config_path(environment)).expanduser().absolute()
    store = trust_store or WorkspaceTrustStore(default_trust_store_path(environment))
    _ensure_store_outside_workspace(store.path, root)
    user_values, user_issues = _read_env_file(user_path, ConfigSource.USER)
    try:
        user_instructions = capture_user_instruction_snapshot(user_path.parent)
        user_instruction_issues: list[ConfigIssue] = []
    except (OSError, UnsafeProjectControl):
        user_instructions = ProjectControlSnapshot(
            workspace_identity=MappingProxyType({
                "lexical": os.path.normcase(os.path.normpath(str(user_path.parent))),
                "resolved": "",
                "device": None,
                "inode": None,
            }),
            fingerprint="",
            files=MappingProxyType({}),
            total_bytes=0,
            trusted=True,
        )
        user_instruction_issues = [ConfigIssue(
            "user-instructions",
            "unsafe user instruction plane; global instructions ignored",
            ConfigSource.USER,
        )]
    workspace_values, workspace_issues = _read_env_file(root / ".env", ConfigSource.WORKSPACE)
    fingerprint = workspace_config_fingerprint(workspace_values)
    try:
        project_control = capture_project_control_snapshot(
            root,
            workspace_config_fingerprint=fingerprint,
        )
        control_fingerprint = project_control.fingerprint
    except (OSError, UnsafeProjectControl):
        control_fingerprint = ""
        try:
            identity = _workspace_identity(root)
        except ConfigValidationError:
            identity = {}
        project_control = ProjectControlSnapshot(
            workspace_identity=MappingProxyType(identity),
            fingerprint="",
            files=MappingProxyType({}),
            total_bytes=0,
        )
        control_issues = [ConfigIssue(
            "workspace-control",
            "unsafe workspace control plane; execution authority ignored",
            ConfigSource.WORKSPACE,
        )]
    else:
        control_issues = []
    try:
        trusted = store.is_trusted(root, "workspace-config", fingerprint)
        trust_issue: list[ConfigIssue] = []
    except ConfigValidationError:
        trusted = False
        trust_issue = [ConfigIssue("workspace-trust", "invalid trust store; workspace values ignored", ConfigSource.USER)]
    try:
        control_trusted = bool(control_fingerprint) and (
            store.is_trusted(
                root,
                "workspace-control",
                control_fingerprint,
            )
            or (trusted and not project_control.files)
        )
    except ConfigValidationError:
        control_trusted = False
        trust_issue.append(ConfigIssue(
            "workspace-control",
            "invalid trust store or control plane; execution authority ignored",
            ConfigSource.USER,
        ))
    if control_trusted:
        project_control = replace(project_control, trusted=True)
    issues = [
        *user_issues,
        *user_instruction_issues,
        *workspace_issues,
        *control_issues,
        *trust_issue,
    ]
    for key in sorted(set(workspace_values) - set(CONFIG_SCHEMA)):
        issues.append(ConfigIssue(
            key,
            "unknown workspace setting ignored",
            ConfigSource.WORKSPACE,
        ))
    keys = {
        key for key, spec in CONFIG_SCHEMA.items()
        if spec.default is not None
        or key in environment
        or key in user_values
        or key in workspace_values
    }
    values: dict[str, ConfigValue] = {}
    for key in keys:
        spec = CONFIG_SCHEMA[key]
        default = "" if spec.default is None else spec.default
        sensitive = spec.workspace_trust_required
        secret = spec.secret
        if key in environment:
            values[key] = ConfigValue(key, str(environment[key]), ConfigSource.ENVIRONMENT, requires_trust=False, secret=secret)
            continue
        if key in user_values:
            values[key] = ConfigValue(key, user_values[key], ConfigSource.USER, requires_trust=False, secret=secret)
            continue
        if key in workspace_values:
            if sensitive and not trusted:
                values[key] = ConfigValue(
                    key,
                    default,
                    ConfigSource.DEFAULT,
                    ignored=True,
                    requires_trust=True,
                    used_default=True,
                    secret=secret,
                )
            else:
                values[key] = ConfigValue(
                    key,
                    workspace_values[key],
                    ConfigSource.TRUSTED_WORKSPACE if sensitive else ConfigSource.WORKSPACE,
                    requires_trust=sensitive,
                    secret=secret,
                )
            continue
        values[key] = ConfigValue(key, default, ConfigSource.DEFAULT, used_default=True, secret=secret)
    values, validation_issues = _validate_captured_values(values)
    snapshot = ConfigSnapshot(
        workspace=root,
        workspace_fingerprint=fingerprint,
        workspace_trusted=trusted,
        control_fingerprint=control_fingerprint,
        control_plane_trusted=control_trusted,
        project_control=project_control,
        values=MappingProxyType(values),
        issues=tuple([*issues, *validation_issues]),
        user_instructions=user_instructions,
    )
    return snapshot


def _validate_captured_values(
    values: dict[str, ConfigValue],
) -> tuple[dict[str, ConfigValue], list[ConfigIssue]]:
    """Validate and select fallbacks before publishing an immutable snapshot."""
    normalized = dict(values)
    issues: list[ConfigIssue] = []
    for key, record in values.items():
        spec = CONFIG_SCHEMA.get(key)
        if spec is None or spec.value_type == "string":
            continue
        valid = True
        message = ""
        try:
            if spec.value_type == "bool":
                valid = record.value.strip().lower() in {
                    "1", "true", "yes", "on", "0", "false", "no", "off",
                }
                message = "invalid boolean; using default"
            elif spec.value_type == "int":
                parsed = int(record.value)
                valid = (
                    (spec.minimum is None or parsed >= spec.minimum)
                    and (spec.maximum is None or parsed <= spec.maximum)
                )
                message = "invalid integer; using default"
            elif spec.value_type == "float":
                parsed = float(record.value)
                valid = (
                    math.isfinite(parsed)
                    and (spec.minimum is None or parsed >= spec.minimum)
                    and (spec.maximum is None or parsed <= spec.maximum)
                )
                message = "invalid finite number; using default"
        except (TypeError, ValueError, OverflowError):
            valid = False
            message = (
                "invalid integer; using default"
                if spec.value_type == "int"
                else "invalid finite number; using default"
            )
        if valid:
            continue
        fallback = "" if spec.default is None else str(spec.default)
        normalized[key] = replace(record, value=fallback, used_default=True)
        issues.append(ConfigIssue(key, message, record.source))
    return normalized, issues


def _read_env_file(path: Path, source: ConfigSource) -> tuple[dict[str, str], list[ConfigIssue]]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, []
    except OSError:
        return {}, [ConfigIssue(path.name, "configuration file cannot be inspected", source)]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return {}, [ConfigIssue(path.name, "configuration file must be a regular non-symlink file", source)]
    if info.st_size > _MAX_CONFIG_BYTES:
        return {}, [ConfigIssue(path.name, "configuration file exceeds 1 MiB", source)]
    try:
        parsed = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError, ValueError):
        return {}, [ConfigIssue(path.name, "configuration file cannot be parsed", source)]
    return {str(key): "" if value is None else str(value) for key, value in parsed.items()}, []


def _workspace_identity(workspace: Path) -> dict[str, object]:
    lexical = Path(workspace).expanduser().absolute()
    normalized_lexical = os.path.normcase(os.path.normpath(str(lexical)))
    try:
        resolved = lexical.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ConfigValidationError("Workspace identity cannot be verified") from exc
    return {
        "lexical": normalized_lexical,
        "resolved": os.path.normcase(os.path.normpath(str(resolved))),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def workspace_identity(workspace: Path | str) -> dict[str, object]:
    """Return the canonical identity shared by all user-owned workspace state."""
    return _workspace_identity(Path(workspace))


def workspace_identity_key(workspace: Path | str) -> str:
    """Return an opaque stable key for the canonical workspace identity."""
    payload = json.dumps(
        workspace_identity(workspace), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trust_key(identity: Mapping[str, object], config_type: str, executable: str) -> str:
    payload = json.dumps(
        {"workspace": dict(identity), "config_type": str(config_type), "executable": str(executable)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_store_outside_workspace(path: Path, workspace: Path) -> None:
    store = path.expanduser().absolute()
    root = workspace.expanduser().absolute().resolve(strict=False)
    try:
        store.resolve(strict=False).relative_to(root)
    except ValueError:
        return
    raise ConfigValidationError("Workspace trust store must be outside the workspace")


__all__ = [
    "ConfigIssue",
    "ConfigSnapshot",
    "ConfigSpec",
    "ConfigSource",
    "ConfigValidationError",
    "ConfigValue",
    "CONFIG_SCHEMA",
    "WorkspaceTrustStore",
    "default_trust_store_path",
    "default_user_config_path",
    "is_secret_config_key",
    "is_sensitive_config_key",
    "active_config_snapshot",
    "current_config_snapshot",
    "load_config_snapshot",
    "scoped_config_snapshot",
    "workspace_config_fingerprint",
    "workspace_control_fingerprint",
    "workspace_identity",
    "workspace_identity_key",
]
