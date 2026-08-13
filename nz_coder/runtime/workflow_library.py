"""Safe discovery and atomic persistence for inert workflow capsules."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from nz_coder import __version__, config
from nz_coder.runtime.workflow_capsule import (
    preflight_workflow_capsule,
    validate_workflow_capsule,
)
from nz_coder.runtime.workdir import current_workdir
from nz_coder.tools import TOOL_HANDLERS, ToolOutput, register


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_CAPSULE_BYTES = 1024 * 1024


def safe_workflow_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("-", str(name).strip()).strip(".-")[:80]
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("workflow name must contain a safe filename character")
    return cleaned


def workflow_library_dirs(
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> dict[str, Path]:
    root = (workspace or current_workdir()).resolve()
    return {
        "project": root / ".nz-coder" / "workflows",
        "personal": (
            personal_dir.resolve()
            if personal_dir is not None
            else Path.home() / ".config" / "nz-coder" / "workflows"
        ),
    }


def discover_workflow_capsules(
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> list[dict]:
    """Discover without parsing; project names override personal names."""
    directories = workflow_library_dirs(workspace, personal_dir)
    found: dict[str, dict] = {}
    for source in ("personal", "project"):
        directory = directories[source]
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.name.endswith(".workflow.json")
            ):
                continue
            name = path.name[:-len(".workflow.json")]
            if not name:
                continue
            found[name] = {
                "name": name,
                "path": str(path),
                "source": source,
                "execution": "capability-generated",
            }
    return [found[name] for name in sorted(found)]


def _read_capsule(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("workflow capsule must be a regular file")
    size = path.stat().st_size
    if size > _MAX_CAPSULE_BYTES:
        raise ValueError("workflow capsule exceeds 1 MiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workflow capsule: {exc}") from exc
    return validate_workflow_capsule(value)


def load_workflow_capsule(
    name: str,
    *,
    workspace: Path | None = None,
    personal_dir: Path | None = None,
    source: str = "",
) -> tuple[dict, dict]:
    safe = safe_workflow_name(name)
    refs = [
        item for item in discover_workflow_capsules(workspace, personal_dir)
        if item["name"] == safe and (not source or item["source"] == source)
    ]
    if not refs:
        raise ValueError(f"saved workflow not found: {safe}")
    if len(refs) > 1:
        raise ValueError(f"ambiguous saved workflow name: {safe}")
    return _read_capsule(Path(refs[0]["path"])), refs[0]


def save_workflow_capsule(
    name: str,
    capsule: dict,
    *,
    scope: str = "project",
    workspace: Path | None = None,
    personal_dir: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """Atomically save one validated, non-executable capsule."""
    if scope not in {"project", "personal"}:
        raise ValueError("workflow scope must be project or personal")
    safe = safe_workflow_name(name)
    validated = validate_workflow_capsule(capsule)
    directory = workflow_library_dirs(workspace, personal_dir)[scope]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe}.workflow.json"
    if (path.exists() or path.is_symlink()) and not overwrite:
        raise ValueError(f"saved workflow already exists: {safe}")
    encoded = (json.dumps(
        validated,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")
    if len(encoded) > _MAX_CAPSULE_BYTES:
        raise ValueError("workflow capsule exceeds 1 MiB")
    temporary = directory / f".{safe}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    return {
        "name": safe,
        "path": str(path),
        "source": scope,
        "execution": "capability-generated",
    }


def _exact_saved_ref(
    name: str,
    *,
    scope: str,
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> tuple[Path, dict]:
    if scope not in {"project", "personal"}:
        raise ValueError("workflow scope must be project or personal")
    safe = safe_workflow_name(name)
    path = workflow_library_dirs(workspace, personal_dir)[scope] / f"{safe}.workflow.json"
    capsule = _read_capsule(path)
    return path, capsule


def rename_workflow_capsule(
    name: str,
    new_name: str,
    *,
    scope: str = "project",
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> dict:
    """Atomically rename one exact saved capsule without changing its bytes."""
    path, _capsule = _exact_saved_ref(
        name, scope=scope, workspace=workspace, personal_dir=personal_dir
    )
    safe = safe_workflow_name(new_name)
    destination = path.parent / f"{safe}.workflow.json"
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"saved workflow already exists: {safe}")
    os.replace(path, destination)
    return {"name": safe, "path": str(destination), "source": scope}


def trash_workflow_capsule(
    name: str,
    *,
    scope: str = "project",
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> dict:
    """Move one exact saved capsule to private recoverable trash."""
    path, _capsule = _exact_saved_ref(
        name, scope=scope, workspace=workspace, personal_dir=personal_dir
    )
    trash = path.parent / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    trash.chmod(0o700)
    destination = trash / f"{path.stem}-{time.time_ns()}.json"
    os.replace(path, destination)
    return {
        "name": safe_workflow_name(name),
        "source": scope,
        "trash_path": str(destination),
        "recoverable": True,
    }


def replace_workflow_capsule(
    name: str,
    capsule: dict,
    *,
    scope: str = "project",
    workspace: Path | None = None,
    personal_dir: Path | None = None,
) -> dict:
    """Replace atomically after preserving the prior validated revision."""
    path, prior = _exact_saved_ref(
        name, scope=scope, workspace=workspace, personal_dir=personal_dir
    )
    validated = validate_workflow_capsule(capsule)
    encoded_new = (json.dumps(
        validated,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")
    if len(encoded_new) > _MAX_CAPSULE_BYTES:
        raise ValueError("workflow capsule exceeds 1 MiB")
    history = path.parent / ".history"
    history.mkdir(parents=True, exist_ok=True)
    history.chmod(0o700)
    revision = history / f"{path.stem}-{time.time_ns()}.json"
    encoded_prior = (json.dumps(prior, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(revision, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded_prior)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    ref = save_workflow_capsule(
        name,
        validated,
        scope=scope,
        workspace=workspace,
        personal_dir=personal_dir,
        overwrite=True,
    )
    return {**ref, "previous_revision": str(revision), "recoverable": True}


def capsule_environment(workspace: Path | None = None) -> dict:
    root = (workspace or current_workdir()).resolve()
    try:
        from nz_coder.state.skills import current_skill_loader

        skills = [item["name"] for item in current_skill_loader().list_skills()]
    except Exception:
        skills = None
    return {
        "nzcoder_version": __version__,
        "is_git_repo": (root / ".git").exists(),
        "worktree_capable": bool(
            config.SUBAGENT_WORKTREE_ENABLED
            and shutil.which("git")
            and (root / ".git").exists()
        ),
        "available_tools": sorted(TOOL_HANDLERS),
        "available_skills": skills,
        "available_model_tiers": ["fast", "balanced", "deep"],
    }


def workflow_library(action: str, name: str = "", source: str = "") -> str:
    """Read-only discovery, inspection, and requirement preflight."""
    try:
        normalized = str(action or "").strip().lower()
        if normalized == "list":
            refs = discover_workflow_capsules()
            return ToolOutput(
                f"Saved workflow capsules: {len(refs)}.",
                title="Workflow library",
                metadata={"workflow_capsules": refs},
            )
        capsule, ref = load_workflow_capsule(name, source=source)
        if normalized == "show":
            return ToolOutput(
                json.dumps(capsule, ensure_ascii=False, indent=2),
                title=f"Workflow capsule: {ref['name']}",
                metadata={"workflow_capsule": capsule, "workflow_ref": ref},
            )
        if normalized == "preflight":
            result = preflight_workflow_capsule(
                capsule,
                capsule_environment(),
            )
            return ToolOutput(
                f"Workflow capsule preflight: {'PASS' if result['ok'] else 'FAIL'}.",
                title="Workflow capsule preflight",
                metadata={"workflow_preflight": result, "workflow_ref": ref},
            )
        return "Error: action must be list, show, or preflight"
    except Exception as exc:
        return f"Error: {exc}"


def workflow_save(
    name: str,
    capsule: dict,
    scope: str = "project",
    overwrite: bool = False,
) -> str:
    """Persist one inert capsule through a write-classified tool."""
    try:
        ref = save_workflow_capsule(
            name,
            capsule,
            scope=scope,
            overwrite=overwrite,
        )
        return ToolOutput(
            f"Saved workflow capsule {ref['name']} in {scope} scope.",
            title="Workflow capsule saved",
            metadata={"workflow_ref": ref},
        )
    except Exception as exc:
        return f"Error: {exc}"


def workflow_library_mutate(
    action: str,
    name: str,
    new_name: str = "",
    capsule: dict | None = None,
    scope: str = "project",
    confirm: bool = False,
) -> str:
    """Rename, replace, or recoverably trash one exact saved capsule."""
    try:
        normalized = str(action or "").strip().lower()
        if normalized == "rename":
            ref = rename_workflow_capsule(name, new_name, scope=scope)
        elif normalized == "replace":
            if not isinstance(capsule, dict):
                return "Error: replace requires capsule"
            ref = replace_workflow_capsule(name, capsule, scope=scope)
        elif normalized == "delete":
            if not confirm:
                return "Error: confirm=true is required to trash a saved workflow"
            ref = trash_workflow_capsule(name, scope=scope)
        else:
            return "Error: action must be rename, replace, or delete"
        return ToolOutput(
            f"Saved workflow {normalized} completed for {name}.",
            title="Workflow library updated",
            metadata={"workflow_ref": ref, "action": normalized},
        )
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="workflow_library",
    description="List, inspect, or preflight saved inert workflow capsules.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "show", "preflight"]},
            "name": {"type": "string"},
            "source": {"type": "string", "enum": ["project", "personal"]},
        },
        "required": ["action"],
    },
    handler=workflow_library,
    execution="read",
)

register(
    name="workflow_save",
    description="Save a validated JSON-only workflow capsule without executable code.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "capsule": {"type": "object"},
            "scope": {"type": "string", "enum": ["project", "personal"]},
            "overwrite": {"type": "boolean"},
        },
        "required": ["name", "capsule"],
    },
    handler=workflow_save,
    execution="write",
)

register(
    name="workflow_library_mutate",
    description="Rename, replace, or recoverably trash one saved JSON workflow capsule.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["rename", "replace", "delete"]},
            "name": {"type": "string"},
            "new_name": {"type": "string"},
            "capsule": {"type": "object"},
            "scope": {"type": "string", "enum": ["project", "personal"]},
            "confirm": {"type": "boolean"},
        },
        "required": ["action", "name"],
    },
    handler=workflow_library_mutate,
    execution="write",
)
