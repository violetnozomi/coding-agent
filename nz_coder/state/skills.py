"""Skill loading: inject domain knowledge on demand from skills/ directories.

改进点（对标 Claude Code loadSkillsDir.ts）：
  - 三级加载路径：project (.nz-coder/skills/) > user (~/.nz-coder/skills/) > bundled (skills/)
  - SKILL.md 支持完整 frontmatter：description, when_to_use, allowed_tools, model, paths
  - when_to_use 字段显示在 descriptions() 中，帮助 agent 选择正确 skill
  - 条件激活（paths frontmatter）：只在操作匹配文件时激活对应 skill
  - lazy 加载：_scan 只读 frontmatter，body 在 load() 时才读取
  - 去重：同名 skill project > user > bundled 优先级
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from nz_coder.foundation import config
from nz_coder.capabilities.ripgrep import RipgrepFilesCancelled, list_ripgrep_files
from nz_coder.state.workdir import current_workdir
from nz_coder.tools import ToolOutput, current_tool_cancel_event, register

if TYPE_CHECKING:
    from nz_coder.foundation.project_control import ProjectControlSnapshot


class _SkillInterrupted(Exception):
    """Internal cooperative stop for skill content/file discovery."""


def _check_skill_cancelled() -> None:
    cancel_event = current_tool_cancel_event()
    if cancel_event is not None and cancel_event.is_set():
        raise _SkillInterrupted


class Skill:
    """A single loaded skill."""
    __slots__ = (
        "name", "description", "when_to_use", "allowed_tools",
        "paths", "source", "file_path", "model", "_body",
    )

    def __init__(
        self,
        name: str,
        description: str,
        when_to_use: str,
        allowed_tools: list[str],
        paths: list[str],
        source: str,
        file_path: Path,
        model: str = "",
        body: str | None = None,
    ):
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.allowed_tools = allowed_tools
        self.paths = paths          # gitignore-style path patterns for conditional activation
        self.source = source        # "project" | "user" | "bundled"
        self.file_path = file_path
        self.model = model
        self._body = body

    def get_body(self) -> str:
        """Load body lazily on first access."""
        _check_skill_cancelled()
        if self._body is None and self.file_path.exists():
            text = self.file_path.read_text(encoding="utf-8")
            _check_skill_cancelled()
            m = re.match(r"^---\s*\n.*?\n---\s*\n(.*)", text, re.DOTALL)
            self._body = m.group(1).strip() if m else text.strip()
        return self._body or ""

    @property
    def base_directory(self) -> Path:
        """Canonical resource base; relative Skill paths may not escape it."""
        return self.file_path.parent.resolve()

    def sample_files(self, limit: int = 10) -> list[Path]:
        """Return a bounded sample of sibling skill resources."""
        directory = self.file_path.parent.resolve()
        try:
            result = list_ripgrep_files(
                directory,
                hidden=True,
                follow=False,
                limit=limit,
                exclude=lambda path: "SKILL.md" in path,
                cancel_event=current_tool_cancel_event(),
            )
        except RipgrepFilesCancelled as error:
            raise _SkillInterrupted from error
        _check_skill_cancelled()
        return [(directory / path).absolute() for path in result.files]


def _parse_skill_file(fp: Path, source: str) -> Optional[Skill]:
    """Parse a SKILL.md file and return a Skill object (body loaded lazily)."""
    try:
        text = fp.read_text(encoding="utf-8")
    except OSError:
        return None

    return _parse_skill_text(text, fp, source, pin_body=False)


def _parse_skill_text(
    text: str,
    fp: Path,
    source: str,
    *,
    pin_body: bool,
) -> Optional[Skill]:
    """Parse metadata and optionally pin the body to already captured text."""
    meta: dict = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()

    name = meta.get("name", fp.parent.name)
    description = meta.get("description", "")
    when_to_use = meta.get("when_to_use", meta.get("when-to-use", ""))

    # allowed_tools: comma-separated string or blank
    raw_tools = meta.get("allowed_tools", meta.get("allowed-tools", ""))
    allowed_tools = [t.strip() for t in raw_tools.split(",") if t.strip()] if raw_tools else []
    if any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", item) for item in allowed_tools):
        return None

    # paths: comma-separated glob patterns for conditional activation
    raw_paths = meta.get("paths", "")
    paths = [p.strip() for p in raw_paths.split(",") if p.strip()] if raw_paths else []
    model = str(meta.get("model") or "").strip()
    if model and not re.fullmatch(r"[A-Za-z0-9_./:-]+", model):
        return None

    body: str | None = None
    if pin_body:
        body_match = re.match(r"^---\s*\n.*?\n---\s*\n(.*)", text, re.DOTALL)
        body = body_match.group(1).strip() if body_match else text.strip()
    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        allowed_tools=allowed_tools,
        paths=paths,
        source=source,
        file_path=fp,
        model=model,
        body=body,
    )


def _scan_skills_dir(skills_dir: Path, source: str) -> list[Skill]:
    """Scan a skills directory and return all valid skills (header-only, lazy body)."""
    if not skills_dir.exists():
        return []
    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            skill_file = entry / "SKILL.md"
            if skill_file.exists() and not skill_file.is_symlink():
                skill = _parse_skill_file(skill_file, source)
                if skill:
                    skills.append(skill)
    return skills


def _scan_project_snapshot(
    snapshot: ProjectControlSnapshot,
    project_dir: Path,
) -> list[Skill]:
    """Build Project Skills only from immutable snapshot bytes."""
    skills: list[Skill] = []
    for item in snapshot.files_for_kind("skill"):
        parts = Path(item.relative_path).parts
        if len(parts) != 4 or parts[:2] != (".nz-coder", "skills"):
            continue
        try:
            text = item.content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        skill = _parse_skill_text(
            text,
            project_dir / parts[2] / "SKILL.md",
            "project",
            pin_body=True,
        )
        if skill is not None:
            skills.append(skill)
    return skills


class SkillLoader:
    """Three-tier skill loader: project > user > bundled.

    Conditional skills (with paths frontmatter) are stored separately and
    activated when the agent operates on matching files.
    """

    def __init__(
        self,
        bundled_dir: Path = None,
        user_dir: Path = None,
        project_dir: Path = None,
        workspace_trusted: bool = True,
        project_control_snapshot: ProjectControlSnapshot | None = None,
    ):
        self._bundled_dir = bundled_dir or config.SKILLS_DIR
        self._user_dir = user_dir or (Path.home() / ".nz-coder" / "skills")
        self._project_dir = project_dir or (current_workdir() / ".nz-coder" / "skills")
        self._project_control_snapshot = project_control_snapshot
        self._workspace_trusted = bool(
            project_control_snapshot is not None
            and project_control_snapshot.trusted
        )

        # name → Skill (unconditional, immediately available)
        self._skills: dict[str, Skill] = {}
        # name → Skill (conditional, waiting for path match)
        self._conditional: dict[str, Skill] = {}
        # file paths already matched (to avoid re-scanning)
        self._activated: set[str] = set()
        self._disabled: dict[str, Skill] = {}
        self._settings_path = self._project_dir.parent / "settings.json"
        self._disabled_names = self._read_disabled_names()

        self._load()

    def _load(self) -> None:
        """Load skills from all tiers, project > user > bundled priority."""
        # Collect from all tiers (higher priority first)
        all_skills: list[Skill] = []
        project_skills = (
            _scan_project_snapshot(
                self._project_control_snapshot,
                self._project_dir,
            )
            if self._workspace_trusted and self._project_control_snapshot is not None
            else []
        )
        sources = [
            (project_skills, "project"),
            (_scan_skills_dir(self._user_dir, "user"), "user"),
            (_scan_skills_dir(self._bundled_dir, "bundled"), "bundled"),
        ]
        for source_skills, source in sources:
            for skill in source_skills:
                all_skills.append(skill)

        # Deduplicate: first occurrence wins (project > user > bundled)
        seen: set[str] = set()
        for skill in all_skills:
            if skill.name in seen:
                continue
            seen.add(skill.name)
            if skill.name in self._disabled_names:
                self._disabled[skill.name] = skill
                continue
            if skill.paths:
                self._conditional[skill.name] = skill
            else:
                self._skills[skill.name] = skill

    def activate_for_paths(self, file_paths: list[str]) -> list[str]:
        """Check conditional skills against file_paths and activate matches.

        Returns names of newly activated skills.
        对标 Claude Code activateConditionalSkillsForPaths()。
        """
        if not self._conditional:
            return []

        newly_activated: list[str] = []
        cwd = str(current_workdir())

        for name, skill in list(self._conditional.items()):
            for fp in file_paths:
                # Normalize to relative path
                rel = fp
                if fp.startswith(cwd):
                    rel = fp[len(cwd):].lstrip("/\\")
                if _matches_any_pattern(rel, skill.paths):
                    self._skills[name] = skill
                    del self._conditional[name]
                    newly_activated.append(name)
                    break

        return newly_activated

    def descriptions(self) -> str:
        """Return skill descriptions for the system prompt.

        改进：包含 when_to_use，帮助 agent 判断何时调用哪个 skill。
        """
        if not self._skills:
            return "(no skills available)"
        lines = []
        for name, skill in sorted(self._skills.items()):
            line = f"- **{name}**: {skill.description}"
            if skill.when_to_use:
                line += f"\n  TRIGGER when: {skill.when_to_use}"
            lines.append(line)
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """Load and return the body of a skill by name."""
        skill = self._skills.get(name)
        if not skill:
            available = ", ".join(sorted(self._skills)) or "none"
            return f"Error: Unknown skill '{name}'. Available: {available}"
        try:
            body = skill.get_body()
            files = skill.sample_files(limit=10)
        except _SkillInterrupted:
            return "Error: Skill loading cancelled"
        except Exception as error:
            return f"Error: Skill loading failed: {error}"
        directory = skill.file_path.parent.resolve()
        escaped_name = html.escape(name, quote=True)
        escaped_source = html.escape(skill.source, quote=True)
        allowed = ""
        if skill.allowed_tools:
            allowed = f'\n<!-- allowed_tools: {", ".join(skill.allowed_tools)} -->'
        file_rows = "\n".join(
            f"<file>{html.escape(str(path))}</file>"
            for path in files
        )
        output = (
            f'<skill_content name="{escaped_name}" source="{escaped_source}">\n'
            f"# Skill: {name}{allowed}\n\n"
            f"{body}\n\n"
            f"Base directory for this skill: {directory.as_uri()}\n"
            "Relative paths in this skill (e.g., scripts/, references/) are "
            "relative to this base directory.\n"
            "Note: file list is sampled.\n\n"
            f"<skill_files>\n{file_rows}\n</skill_files>\n"
            "</skill_content>"
        )
        metadata = {"name": name, "dir": str(directory)}
        if skill.model:
            metadata.update({"model": skill.model, "source": skill.source})
        _activate_skill(skill)
        return ToolOutput(
            output,
            title=f"Loaded skill: {name}",
            metadata=metadata,
        )

    def get_skill_info(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self, *, include_conditional: bool = True) -> list[dict]:
        """Return secret-free metadata for the unified extension registry."""
        items = [
            {
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "allowed_tools": list(skill.allowed_tools),
                "paths": list(skill.paths),
                "model": skill.model,
                "status": "available",
            }
            for skill in self._skills.values()
        ]
        if include_conditional:
            items.extend(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "allowed_tools": list(skill.allowed_tools),
                    "paths": list(skill.paths),
                    "model": skill.model,
                    "status": "conditional",
                }
                for skill in self._conditional.values()
            )
            items.extend(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "allowed_tools": list(skill.allowed_tools),
                    "paths": list(skill.paths),
                    "model": skill.model,
                    "status": "disabled",
                }
                for skill in self._disabled.values()
            )
        return sorted(items, key=lambda item: item["name"])

    def set_enabled(self, name: str, enabled: bool) -> str:
        """Persist one Skill preference in user-owned workspace state."""
        selected = str(name).strip()
        known = set(self._skills) | set(self._conditional) | set(self._disabled)
        if selected not in known:
            raise ValueError(f"Unknown skill '{selected}'")
        from nz_coder.tool_platform.permissioning.grants import UserGrantStore

        UserGrantStore().set_skill_enabled(
            self._project_dir.parent.parent,
            selected,
            enabled,
        )
        self.reload()
        item = next(entry for entry in self.list_skills() if entry["name"] == selected)
        return str(item["status"])

    def reload(
        self,
        project_control_snapshot: ProjectControlSnapshot | None = None,
    ) -> None:
        """Reload with a new snapshot or recapture and re-check Project Trust."""
        if project_control_snapshot is None:
            from nz_coder.foundation.workspace_trust import load_config_snapshot

            project_control_snapshot = load_config_snapshot(
                self._project_dir.parent.parent
            ).project_control
        self._project_control_snapshot = project_control_snapshot
        self._workspace_trusted = bool(project_control_snapshot.trusted)
        self._skills.clear()
        self._conditional.clear()
        self._activated.clear()
        self._disabled.clear()
        self._disabled_names = self._read_disabled_names()
        self._load()

    def _read_settings(self) -> dict:
        item = (
            self._project_control_snapshot.get(".nz-coder/settings.json")
            if self._project_control_snapshot is not None
            else None
        )
        if item is None:
            return {}
        try:
            value = json.loads(item.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid skill settings") from exc
        if not isinstance(value, dict):
            raise ValueError("Skill settings must be a JSON object")
        return value

    def _read_disabled_names(self) -> set[str]:
        values = (
            self._read_settings().get("disabled_skills", [])
            if self._workspace_trusted else []
        )
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError("disabled_skills must be a list of names")
        project_disabled = {item.strip() for item in values if item.strip()}
        try:
            from nz_coder.tool_platform.permissioning.grants import UserGrantStore

            user_disabled = UserGrantStore().load_disabled_skills(
                self._project_dir.parent.parent
            )
        except (OSError, PermissionError, ValueError):
            user_disabled = set()
        return project_disabled | user_disabled

def _matches_any_pattern(rel_path: str, patterns: list[str]) -> bool:
    """Check if rel_path matches any gitignore-style pattern.

    Simple implementation: supports * wildcard and ** glob.
    For production use, consider the `pathspec` library.
    """
    import fnmatch
    for pattern in patterns:
        # Normalize pattern
        if pattern.endswith("/**"):
            pattern = pattern[:-3]
        # Direct match or prefix match
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if fnmatch.fnmatch(rel_path, pattern + "/*"):
            return True
        if rel_path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


# ── Global instance ───────────────────────────────────────────────────────────

skill_loader = SkillLoader(
    workspace_trusted=getattr(config.CONFIG_SNAPSHOT, "control_plane_trusted", False),
    project_control_snapshot=getattr(config.CONFIG_SNAPSHOT, "project_control", None),
)
_SKILL_LOADER: ContextVar[SkillLoader | None] = ContextVar(
    "nz_coder_skill_loader",
    default=None,
)
_SKILL_EXECUTION: ContextVar[SkillExecutionContext | None] = ContextVar(
    "nz_coder_skill_execution_context", default=None,
)


@dataclass
class SkillExecutionContext:
    """Run-local governed effects activated by loaded Skills."""

    active_skills: tuple[str, ...] = ()
    allowed_tools: frozenset[str] | None = None
    model_preferences: tuple[str, ...] = ()
    provenance: list[dict] = field(default_factory=list)

    def activate(self, skill: Skill) -> None:
        if skill.name not in self.active_skills:
            self.active_skills = (*self.active_skills, skill.name)
        if skill.allowed_tools:
            declared = frozenset(skill.allowed_tools)
            self.allowed_tools = (
                declared if self.allowed_tools is None else self.allowed_tools & declared
            )
        if skill.model and skill.model not in self.model_preferences:
            self.model_preferences = (*self.model_preferences, skill.model)
        self.provenance.append({
            "name": skill.name,
            "source": skill.source,
            "path": str(skill.file_path.resolve()),
        })


def current_skill_execution_context() -> SkillExecutionContext:
    context = _SKILL_EXECUTION.get()
    if context is None:
        raise RuntimeError("No active Skill execution context")
    return context


def current_skill_allowed_tools() -> frozenset[str] | None:
    context = _SKILL_EXECUTION.get()
    return context.allowed_tools if context is not None else None


def _activate_skill(skill: Skill) -> None:
    context = _SKILL_EXECUTION.get()
    if context is not None:
        context.activate(skill)


def current_skill_loader() -> SkillLoader:
    """Return the skill loader bound to the current agent context."""
    return _SKILL_LOADER.get() or skill_loader


@contextmanager
def bind_skill_loader(loader: SkillLoader):
    """Temporarily bind a skill loader to the current execution context."""
    token = _SKILL_LOADER.set(loader)
    execution_token = _SKILL_EXECUTION.set(SkillExecutionContext())
    try:
        yield loader
    finally:
        _SKILL_EXECUTION.reset(execution_token)
        _SKILL_LOADER.reset(token)


def _load_skill(name: str) -> str:
    return current_skill_loader().load(name)


register(
    name="load_skill",
    description=(
        "Load specialized domain knowledge or workflow instructions by skill name. "
        "Check the available skills list in the system prompt before calling."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name to load (e.g. 'code-review')."},
        },
        "required": ["name"],
    },
    handler=_load_skill,
    plan_mode_allowed=True,
)
