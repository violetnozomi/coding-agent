"""Track agent-authored file changes for diff review and rollback."""
from __future__ import annotations


import difflib
import json
import time
import uuid
from pathlib import Path

from nz_coder import config


class ChangeTracker:
    """Persist before/after text snapshots for files changed by the agent."""

    def __init__(self, run_id: str = None, change_dir: Path = None, enabled: bool = True):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.change_dir = change_dir or config.CHANGE_DIR
        self.enabled = enabled
        self.path = self.change_dir / f"{self.run_id}.json"
        self._changes: dict[str, dict] = {}
        if self.enabled:
            self.change_dir.mkdir(parents=True, exist_ok=True)

    def record_before(self, rel_path: str, exists: bool, content: str = "") -> None:
        if not self.enabled:
            return
        # FIXED: after_exists/after 初始化为 None，而非复制 before 的值。
        # 若 record_after 未被调用（工具异常中断），None 明确表示「after 状态未知」，
        # 而不是错误地沿用 before 的值。
        item = self._changes.setdefault(str(rel_path), {
            "path": str(rel_path),
            "before_exists": bool(exists),
            "before": content or "",
            "after_exists": None,
            "after": None,
        })
        if "before" not in item:
            item["before_exists"] = bool(exists)
            item["before"] = content or ""
        self._save()

    def record_after(self, rel_path: str, exists: bool, content: str = "") -> None:
        if not self.enabled:
            return
        item = self._changes.setdefault(str(rel_path), {
            "path": str(rel_path),
            "before_exists": False,
            "before": "",
        })
        item["after_exists"] = bool(exists)
        item["after"] = content or ""
        self._save()

    def render_diff(self) -> str:
        return render_change_diff(load_change_file(self.path))

    def revert(self) -> str:
        return revert_change_file(self.path)

    def _save(self) -> None:
        payload = {
            "run_id": self.run_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": str(config.WORKDIR),
            "changes": list(self._changes.values()),
        }
        self.change_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def latest_change_file(change_dir: Path = None) -> Path | None:
    base = change_dir or config.CHANGE_DIR
    if not base.exists():
        return None
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_change_file(path: Path | None = None) -> dict:
    target = path or latest_change_file()
    if not target or not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def render_latest_diff() -> str:
    return render_change_diff(load_change_file())


def render_change_diff(payload: dict) -> str:
    if not payload or not payload.get("changes"):
        return "No agent file changes recorded."
    sections = [f"Change set: {payload.get('run_id', '-')}", f"Workspace: {payload.get('workspace', '-')}", ""]
    for change in payload["changes"]:
        before = change.get("before", "") if change.get("before_exists") else ""
        # FIXED: after 为 None 表示工具中断、after 状态未记录，渲染为空字符串。
        after_exists = change.get("after_exists")
        after = (change.get("after") or "") if after_exists else ""
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{change['path']}",
            tofile=f"b/{change['path']}",
        ))
        sections.append(f"## {change['path']}")
        sections.append(diff or "(no changes)")
    return "\n".join(sections).rstrip()


def revert_latest() -> str:
    return revert_change_file(latest_change_file())


def revert_change_file(path: Path | None) -> str:
    payload = load_change_file(path)
    if not payload or not payload.get("changes"):
        return "No agent file changes recorded."
    restored = []
    refused = []
    for change in payload["changes"]:
        rel_path = change["path"]
        target = _safe_path(rel_path)
        current_exists = target.exists()
        current = target.read_text(encoding="utf-8", errors="replace") if current_exists else ""
        # FIXED: after_exists=None 表示中断，视为不满足 current state 匹配，拒绝回滚。
        after_exists = change.get("after_exists")
        if after_exists is None:
            refused.append(f"{rel_path} (after state was never recorded — tool may have been interrupted)")
            continue
        expected_exists = bool(after_exists)
        expected = change.get("after") or "" if expected_exists else ""
        if current_exists != expected_exists or current != expected:
            refused.append(f"{rel_path} (current content no longer matches tracked after-state)")
            continue
        if change.get("before_exists"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.get("before", ""), encoding="utf-8")
            restored.append(f"restored {rel_path}")
        else:
            if target.exists():
                target.unlink()
            restored.append(f"deleted {rel_path}")
    if refused:
        return "Refused to revert:\n" + "\n".join(f"- {p}" for p in refused)
    return "Reverted agent changes:\n" + "\n".join(f"- {p}" for p in restored)


def _safe_path(p: str) -> Path:
    path = (config.WORKDIR / p).resolve()
    try:
        path.relative_to(config.WORKDIR.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {p}")
    return path
