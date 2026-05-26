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

import re
from pathlib import Path
from typing import Optional

from nz_coder import config
from nz_coder.tools import register


class Skill:
    """A single loaded skill."""
    __slots__ = (
        "name", "description", "when_to_use", "allowed_tools",
        "paths", "source", "file_path", "_body",
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
        body: str = "",
    ):
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.allowed_tools = allowed_tools
        self.paths = paths          # gitignore-style path patterns for conditional activation
        self.source = source        # "project" | "user" | "bundled"
        self.file_path = file_path
        self._body = body           # empty until load() is called (lazy)

    def get_body(self) -> str:
        """Load body lazily on first access."""
        if not self._body and self.file_path.exists():
            text = self.file_path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n.*?\n---\s*\n(.*)", text, re.DOTALL)
            self._body = m.group(1).strip() if m else text.strip()
        return self._body


def _parse_skill_file(fp: Path, source: str) -> Optional[Skill]:
    """Parse a SKILL.md file and return a Skill object (body loaded lazily)."""
    try:
        text = fp.read_text(encoding="utf-8")
    except OSError:
        return None

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

    # paths: comma-separated glob patterns for conditional activation
    raw_paths = meta.get("paths", "")
    paths = [p.strip() for p in raw_paths.split(",") if p.strip()] if raw_paths else []

    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        allowed_tools=allowed_tools,
        paths=paths,
        source=source,
        file_path=fp,
    )


def _scan_skills_dir(skills_dir: Path, source: str) -> list[Skill]:
    """Scan a skills directory and return all valid skills (header-only, lazy body)."""
    if not skills_dir.exists():
        return []
    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir():
            skill_file = entry / "SKILL.md"
            if skill_file.exists():
                skill = _parse_skill_file(skill_file, source)
                if skill:
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
    ):
        self._bundled_dir = bundled_dir or config.SKILLS_DIR
        self._user_dir = user_dir or (Path.home() / ".nz-coder" / "skills")
        self._project_dir = project_dir or (config.WORKDIR / ".nz-coder" / "skills")

        # name → Skill (unconditional, immediately available)
        self._skills: dict[str, Skill] = {}
        # name → Skill (conditional, waiting for path match)
        self._conditional: dict[str, Skill] = {}
        # file paths already matched (to avoid re-scanning)
        self._activated: set[str] = set()

        self._load()

    def _load(self) -> None:
        """Load skills from all tiers, project > user > bundled priority."""
        # Collect from all tiers (higher priority first)
        all_skills: list[Skill] = []
        for skills_dir, source in [
            (self._project_dir, "project"),
            (self._user_dir, "user"),
            (self._bundled_dir, "bundled"),
        ]:
            for skill in _scan_skills_dir(skills_dir, source):
                all_skills.append(skill)

        # Deduplicate: first occurrence wins (project > user > bundled)
        seen: set[str] = set()
        for skill in all_skills:
            if skill.name in seen:
                continue
            seen.add(skill.name)
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
        cwd = str(config.WORKDIR)

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
        body = skill.get_body()
        header = f'<skill name="{name}" source="{skill.source}">'
        if skill.allowed_tools:
            header += f'\n<!-- allowed_tools: {", ".join(skill.allowed_tools)} -->'
        return f"{header}\n{body}\n</skill>"

    def get_skill_info(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def reload(self) -> None:
        """Reload all skills (e.g. after file changes)."""
        self._skills.clear()
        self._conditional.clear()
        self._activated.clear()
        self._load()


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

skill_loader = SkillLoader()


def _load_skill(name: str) -> str:
    return skill_loader.load(name)


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
)
