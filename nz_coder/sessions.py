"""Session persistence for conversation resume."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from nz_coder import config


def save_session(messages: list, mode: str = None, session_id: str = None) -> Path:
    session_id = session_id or time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = config.SESSION_DIR / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace": str(config.WORKDIR),
        "model": config.MODEL_ID,
        "mode": mode or config.PERMISSION_MODE,
        "messages": messages,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    latest = config.SESSION_DIR / "latest.json"
    latest.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def load_session(session_id: str = "latest") -> dict:
    path = _session_path(session_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def list_sessions(limit: int = 10) -> list[Path]:
    if not config.SESSION_DIR.exists():
        return []
    files = [p for p in config.SESSION_DIR.glob("*.json") if p.name != "latest.json"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def describe_sessions(limit: int = 10) -> str:
    sessions = list_sessions(limit)
    if not sessions:
        return "No saved sessions."
    lines = ["Saved sessions:"]
    for path in sessions:
        payload = load_session(path.stem)
        msg_count = len(payload.get("messages", []))
        lines.append(f"- {path.stem}: {payload.get('timestamp', '-')} ({msg_count} messages)")
    return "\n".join(lines)


def _session_path(session_id: str) -> Path:
    if not session_id or session_id == "latest":
        return config.SESSION_DIR / "latest.json"
    safe = "".join(c for c in session_id if c.isalnum() or c in ("_", "-"))
    return config.SESSION_DIR / f"{safe}.json"
