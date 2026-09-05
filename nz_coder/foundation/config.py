"""Configuration management shared by all NZ-Coder product surfaces."""

from pathlib import Path
import re

from nz_coder.foundation.workspace_trust import (
    load_config_snapshot,
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _PACKAGE_ROOT.parent
WORKSPACE_ENV_PATH = Path.cwd() / ".env"
SOURCE_ENV_PATH = _PROJECT_ROOT / ".env"
CONFIG_SNAPSHOT = load_config_snapshot(Path.cwd())
CONFIG_ISSUES = CONFIG_SNAPSHOT.issues

_INTEGER_DEFAULT = re.compile(r"[+-]?\d+")
_FLOAT_DEFAULT = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
)


def get(key: str, default: str = None) -> str:
    """Read one startup-snapshot value with import-safe numeric fallback."""
    fallback = "" if default is None else str(default)
    if _INTEGER_DEFAULT.fullmatch(fallback):
        return str(CONFIG_SNAPSHOT.get_int(key, int(fallback)))
    if _FLOAT_DEFAULT.fullmatch(fallback) and any(char in fallback for char in ".eE"):
        return str(CONFIG_SNAPSHOT.get_float(key, float(fallback)))
    return CONFIG_SNAPSHOT.get(key, fallback)


API_KEY = get("API_KEY", "")
DEFAULT_MODEL_ID = "deepseek-v4-flash"
DEFAULT_API_BASE_URL = "https://api.deepseek.com"
MODEL_ID = get("MODEL_ID", DEFAULT_MODEL_ID)
API_BASE_URL = get("API_BASE_URL", DEFAULT_API_BASE_URL)
MODEL_PROVIDER = get("MODEL_PROVIDER", "openai-compatible")
MODEL_CAPABILITIES_JSON = get("MODEL_CAPABILITIES_JSON", "")
MODEL_CATALOG_JSON = get("MODEL_CATALOG_JSON", "")
MODEL_CATALOG_PATH = get("MODEL_CATALOG_PATH", "")
MODEL_VARIANT = get("MODEL_VARIANT", "")
PROVIDER_MAX_RETRIES = max(0, int(get("NZ_PROVIDER_MAX_RETRIES", "3")))
PROVIDER_HARD_TIMEOUT_SECONDS = max(
    1.0, float(get("NZ_PROVIDER_HARD_TIMEOUT_SECONDS", "600"))
)
PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS = max(
    0.0, float(get("NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS", "60"))
)
PROVIDER_CANCEL_GRACE_SECONDS = min(
    5.0,
    max(0.0, float(get("NZ_PROVIDER_CANCEL_GRACE_SECONDS", "0.25"))),
)
STREAM_CHECKPOINT_INTERVAL_SECONDS = max(
    0.05,
    float(get("NZ_STREAM_CHECKPOINT_INTERVAL_SECONDS", "0.5")),
)
STREAM_CHECKPOINT_MIN_CHARS = max(
    256,
    int(get("NZ_STREAM_CHECKPOINT_MIN_CHARS", "4096")),
)
STREAM_DELTA_INTERVAL_SECONDS = min(
    0.08,
    max(0.03, float(get("NZ_STREAM_DELTA_INTERVAL_SECONDS", "0.05"))),
)
STREAM_DELTA_MIN_CHARS = max(
    32,
    int(get("NZ_STREAM_DELTA_MIN_CHARS", "256")),
)
REMOTE_EVENT_QUEUE_SIZE = max(
    32,
    int(get("NZ_REMOTE_EVENT_QUEUE_SIZE", "512")),
)
PROVIDER_NON_STREAMING_FALLBACK = get(
    "NZ_PROVIDER_NON_STREAMING_FALLBACK", "1"
).lower() in ("1", "true", "yes", "on")
IMAGE_DESCRIBE_PROVIDER = get("NZ_IMAGE_DESCRIBE_PROVIDER", MODEL_PROVIDER)
IMAGE_DESCRIBE_MODEL = get("NZ_IMAGE_DESCRIBE_MODEL", "")
IMAGE_DESCRIBE_API_KEY = get("NZ_IMAGE_DESCRIBE_API_KEY", "")
IMAGE_DESCRIBE_BASE_URL = get("NZ_IMAGE_DESCRIBE_BASE_URL", "")
IMAGE_DESCRIBE_MAX_TOKENS = int(get("NZ_IMAGE_DESCRIBE_MAX_TOKENS", "1200"))
MODEL_REGISTRY_URL = get("NZ_MODEL_REGISTRY_URL", "https://models.dev/api.json")
MODEL_REGISTRY_PATH = get("NZ_MODEL_REGISTRY_PATH", ".nz-coder/models/registry.json")
MODEL_REGISTRY_TTL_SECONDS = int(get("NZ_MODEL_REGISTRY_TTL_SECONDS", "300"))
OPENAI_API_KEY = get("OPENAI_API_KEY", API_KEY)
OPENAI_API_BASE_URL = get("OPENAI_API_BASE_URL", "https://api.openai.com/v1")
MCP_ENABLED = get("NZ_MCP_ENABLED", "0").lower() in ("1", "true", "yes", "on")
MCP_SERVERS_JSON = get("NZ_MCP_SERVERS_JSON", "")
MCP_USER_CONFIG = get(
    "NZ_MCP_USER_CONFIG",
    str(Path.home() / ".config" / "nz-coder" / "mcp.json"),
)
MCP_PROJECT_CONFIG = get("NZ_MCP_PROJECT_CONFIG", ".nz-coder/mcp.json")
MCP_TRUST_STORE = get(
    "NZ_MCP_TRUST_STORE",
    str(Path.home() / ".config" / "nz-coder" / "mcp-trust.json"),
)
MCP_STARTUP_TIMEOUT_SECONDS = float(get("NZ_MCP_STARTUP_TIMEOUT_SECONDS", "30"))
MCP_TOOL_TIMEOUT_SECONDS = float(get("NZ_MCP_TOOL_TIMEOUT_SECONDS", "30"))
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY", API_KEY)
ANTHROPIC_API_BASE_URL = get("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_API_VERSION = get("ANTHROPIC_API_VERSION", "2023-06-01")
GEMINI_API_KEY = get("GEMINI_API_KEY", API_KEY)
GEMINI_API_BASE_URL = get(
    "GEMINI_API_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta",
)
PERMISSION_MODE = get("PERMISSION_MODE", "default")
AUTO_MODE_CLASSIFIER_ENABLED = get(
    "NZ_AUTO_MODE_CLASSIFIER_ENABLED", "1"
).lower() in ("1", "true", "yes", "on")
AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS = max(
    1.0, float(get("NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS", "15"))
)
AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS = max(
    64, int(get("NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS", "256"))
)
AUTO_MODE_CLASSIFIER_BLOCK_STREAK = max(
    1, int(get("NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK", "3"))
)
AUTO_MODE_CLASSIFIER_INFRA_FAILURES = max(
    1, int(get("NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES", "5"))
)
AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS = max(
    1.0, float(get("NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS", "600"))
)
MAX_CONTEXT_TOKENS = int(get("MAX_CONTEXT_TOKENS", "100000"))
MAX_OUTPUT_TOKENS = int(get("MAX_OUTPUT_TOKENS", "8000"))
DEFAULT_CONTEXT_REPLAY_COMPACTION_TOKENS = 0
CONTEXT_REPLAY_COMPACTION_TOKENS = max(
    0,
    int(get(
        "NZ_CONTEXT_REPLAY_COMPACTION_TOKENS",
        str(DEFAULT_CONTEXT_REPLAY_COMPACTION_TOKENS),
    )),
)
LOG_LEVEL = get("LOG_LEVEL", "INFO")
MAX_AGENT_TURNS = int(get("MAX_AGENT_TURNS", "500"))
NOMINAL_AGENT_TURNS = max(1, int(get("NZ_NOMINAL_AGENT_TURNS", "200")))
SWE_NOMINAL_AGENT_TURNS = max(
    1, int(get("NZ_SWE_NOMINAL_AGENT_TURNS", "200"))
)
MAX_TOOL_CALLS_PER_RESPONSE = int(get("MAX_TOOL_CALLS_PER_RESPONSE", "20"))
DOOM_LOOP_THRESHOLD = int(get("NZ_DOOM_LOOP_THRESHOLD", "3"))
READ_DEDUP_ENABLED = get("NZ_READ_DEDUP_ENABLED", "1").lower() in (
    "1", "true", "yes", "on",
)
CONTINUE_LOOP_ON_DENY = get("NZ_CONTINUE_LOOP_ON_DENY", "0").lower() in (
    "1", "true", "yes", "on",
)
MAX_PARALLEL_TASKS = int(get("MAX_PARALLEL_TASKS", "4"))
MAX_VERIFICATION_GATE_PROMPTS = int(get("MAX_VERIFICATION_GATE_PROMPTS", "2"))
BASH_TIMEOUT_SECONDS = int(get("BASH_TIMEOUT_SECONDS", "120"))
PROCESS_BUFFER_BYTES = int(get("NZ_PROCESS_BUFFER_BYTES", str(2 * 1024 * 1024)))
BASH_OUTPUT_HARD_LIMIT_BYTES = max(
    PROCESS_BUFFER_BYTES,
    int(get("NZ_BASH_OUTPUT_HARD_LIMIT_BYTES", str(64 * 1024 * 1024))),
)
PROCESS_READ_MAX_BYTES = int(get("NZ_PROCESS_READ_MAX_BYTES", str(64 * 1024)))
PROCESS_WRITE_MAX_BYTES = int(get("NZ_PROCESS_WRITE_MAX_BYTES", str(64 * 1024)))
PROCESS_MAX_PER_WORKSPACE = int(get("NZ_PROCESS_MAX_PER_WORKSPACE", "16"))
PROCESS_KILL_GRACE_SECONDS = float(get("NZ_PROCESS_KILL_GRACE_SECONDS", "0.5"))
PROCESS_OUTPUT_ENCODING = get("NZ_PROCESS_OUTPUT_ENCODING", "").strip() or None
LSP_ENABLED = get("NZ_LSP_ENABLED", "1").lower() in ("1", "true", "yes", "on")
LSP_INITIALIZE_TIMEOUT_SECONDS = float(get("NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", "20"))
LSP_REQUEST_TIMEOUT_SECONDS = float(get("NZ_LSP_REQUEST_TIMEOUT_SECONDS", "10"))
LSP_DIAGNOSTIC_WAIT_SECONDS = float(get("NZ_LSP_DIAGNOSTIC_WAIT_SECONDS", "2"))
LSP_MAX_OUTPUT_CHARS = int(get("NZ_LSP_MAX_OUTPUT_CHARS", "20000"))
LSP_WRITE_DIAGNOSTICS_ENABLED = get(
    "NZ_LSP_WRITE_DIAGNOSTICS_ENABLED", "1"
).lower() in ("1", "true", "yes", "on")
LSP_WRITE_DIAGNOSTIC_MAX_FILES = int(
    get("NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES", "8")
)
REPO_MAP_MAX_FILES = int(get("NZ_REPO_MAP_MAX_FILES", "80"))
REPO_MAP_MAX_SYMBOLS = int(get("NZ_REPO_MAP_MAX_SYMBOLS", "600"))
REPO_MAP_MAX_FILE_BYTES = int(get("NZ_REPO_MAP_MAX_FILE_BYTES", "1000000"))
TRACE_ENABLED = get("TRACE_ENABLED", "1").lower() in ("1", "true", "yes", "on")
ALLOW_BASH_PACKAGE_INSTALLS = get("ALLOW_BASH_PACKAGE_INSTALLS", "0").lower() in ("1", "true", "yes", "on")
MEMORY_LLM_RERANK = get("MEMORY_LLM_RERANK", "0").lower() in ("1", "true", "yes", "on")
MEMORY_LLM_EXTRACT = get("MEMORY_LLM_EXTRACT", "0").lower() in ("1", "true", "yes", "on")
MEMORY_ASYNC_WRITE = get("MEMORY_ASYNC_WRITE", "0").lower() in ("1", "true", "yes", "on")
MEMORY_AUTO_EXTRACT = get("MEMORY_AUTO_EXTRACT", "1").lower() in ("1", "true", "yes", "on")
MEMORY_AUTO_DREAM = get("MEMORY_AUTO_DREAM", "1").lower() in ("1", "true", "yes", "on")
MEMORY_AUTO_DREAM_MIN_HOURS = int(get("MEMORY_AUTO_DREAM_MIN_HOURS", "24"))
MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS = int(get("MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS", "5"))
MEMORY_CLEANUP_DAYS = int(get("MEMORY_CLEANUP_DAYS", "30"))
SYSTEM_CONTEXT_BUDGET_TOKENS = int(get("SYSTEM_CONTEXT_BUDGET_TOKENS", "6000"))
RUNTIME_STATE_PERSIST = get("RUNTIME_STATE_PERSIST", "1").lower() in ("1", "true", "yes", "on")
SUBAGENT_MAX_TURNS = int(get("SUBAGENT_MAX_TURNS", "200"))
SUBAGENT_TIMEOUT_SECONDS = int(get("SUBAGENT_TIMEOUT_SECONDS", "180"))
SUBAGENT_EXPLORE_MODEL = get("SUBAGENT_EXPLORE_MODEL", "")
SUBAGENT_DEEP_MODEL = get("SUBAGENT_DEEP_MODEL", "")
SUBAGENT_WORKTREE_ENABLED = get("SUBAGENT_WORKTREE_ENABLED", "1").lower() in ("1", "true", "yes", "on")
SUBAGENT_BACKGROUND_MAX_TASKS = int(get("SUBAGENT_BACKGROUND_MAX_TASKS", "20"))
SUBAGENT_BACKGROUND_MAX_CONCURRENT = int(get("SUBAGENT_BACKGROUND_MAX_CONCURRENT", "4"))
SUBAGENT_PROCESS_ISOLATION_ENABLED = get(
    "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", "1"
).lower() in ("1", "true", "yes", "on")
SUBAGENT_PROCESS_STOP_GRACE_SECONDS = float(get(
    "NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS", "0.5"
))
REFLECTION_ENABLED = get("NZ_REFLECTION_ENABLED", "0").lower() in ("1", "true", "yes", "on")
REFLECTION_MAX_ATTEMPTS = int(get("NZ_REFLECTION_MAX_ATTEMPTS", "2"))

# Planning / Replanning. 默认关闭，避免改变现有测试和基准行为。
PLANNING_ENABLED = get("NZ_PLANNING_ENABLED", "").lower() in ("1", "true", "yes", "on")
PLANNING_TASK_MODES = {"feature", "refactor", "test", "project_creation"}
REPLAN_IDLE_TURNS = int(get("NZ_REPLAN_IDLE_TURNS", "5"))
REPLAN_MAX_ATTEMPTS = int(get("NZ_REPLAN_MAX_ATTEMPTS", "2"))
PLANNING_MAX_TOKENS = int(get("NZ_PLANNING_MAX_TOKENS", "1500"))
WRITE_BATCH_MAX_FILE_BYTES = int(get("NZ_WRITE_BATCH_MAX_FILE_BYTES", "100000"))
WRITE_BATCH_MAX_TOTAL_BYTES = int(get("NZ_WRITE_BATCH_MAX_TOTAL_BYTES", "500000"))
PROJECT_VERIFY_TIMEOUT_SECONDS = int(get("NZ_PROJECT_VERIFY_TIMEOUT_SECONDS", "60"))

# Workspace is the current working directory when the agent starts
WORKDIR = Path.cwd()

# Internal directories
TRANSCRIPT_DIR = WORKDIR / ".nz-coder" / "transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".nz-coder" / "tool-results"
MEMORY_DIR = WORKDIR / ".nz-coder" / "memory"
TRACE_DIR = WORKDIR / ".nz-coder" / "runs"
CHANGE_DIR = WORKDIR / ".nz-coder" / "changes"
SESSION_DIR = WORKDIR / ".nz-coder" / "sessions"
SKILLS_DIR = _PACKAGE_ROOT / "bundled_skills"

# Context control
PERSIST_OUTPUT_TRIGGER = 30000
PERSIST_PREVIEW_CHARS = 2000
CONTEXT_TRUNCATE_CHARS = 50000
