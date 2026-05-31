"""Configuration management."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from nz-coder project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)


def get(key: str, default: str = None) -> str:
    return os.environ.get(key, default)


API_KEY = get("API_KEY", "")
MODEL_ID = get("MODEL_ID", "qwen-plus")
API_BASE_URL = get("API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
PERMISSION_MODE = get("PERMISSION_MODE", "default")
MAX_CONTEXT_TOKENS = int(get("MAX_CONTEXT_TOKENS", "100000"))
LOG_LEVEL = get("LOG_LEVEL", "INFO")
MAX_AGENT_TURNS = int(get("MAX_AGENT_TURNS", "50"))
MAX_TOOL_CALLS_PER_RESPONSE = int(get("MAX_TOOL_CALLS_PER_RESPONSE", "20"))
MAX_VERIFICATION_GATE_PROMPTS = int(get("MAX_VERIFICATION_GATE_PROMPTS", "2"))
BASH_TIMEOUT_SECONDS = int(get("BASH_TIMEOUT_SECONDS", "120"))
TRACE_ENABLED = get("TRACE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
ALLOW_BASH_PACKAGE_INSTALLS = get("ALLOW_BASH_PACKAGE_INSTALLS", "0").lower() in ("1", "true", "yes", "on")
MEMORY_LLM_RERANK = get("MEMORY_LLM_RERANK", "0").lower() in ("1", "true", "yes", "on")
MEMORY_LLM_EXTRACT = get("MEMORY_LLM_EXTRACT", "0").lower() in ("1", "true", "yes", "on")
MEMORY_ASYNC_WRITE = get("MEMORY_ASYNC_WRITE", "0").lower() in ("1", "true", "yes", "on")
SYSTEM_CONTEXT_BUDGET_TOKENS = int(get("SYSTEM_CONTEXT_BUDGET_TOKENS", "6000"))
RUNTIME_STATE_PERSIST = get("RUNTIME_STATE_PERSIST", "1").lower() in ("1", "true", "yes", "on")
SUBAGENT_MAX_TURNS = int(get("SUBAGENT_MAX_TURNS", "12"))
SUBAGENT_TIMEOUT_SECONDS = int(get("SUBAGENT_TIMEOUT_SECONDS", "180"))

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
SKILLS_DIR = _PROJECT_ROOT / "skills"

# Context control
PERSIST_OUTPUT_TRIGGER = 30000
PERSIST_PREVIEW_CHARS = 2000
CONTEXT_TRUNCATE_CHARS = 50000
KEEP_RECENT_TOOL_RESULTS = 3
