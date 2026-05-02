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
BASH_TIMEOUT_SECONDS = int(get("BASH_TIMEOUT_SECONDS", "120"))
TRACE_ENABLED = get("TRACE_ENABLED", "0").lower() in ("1", "true", "yes", "on")

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
