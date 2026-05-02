"""Skill loading: inject domain knowledge on demand from skills/ directory."""

import re
from pathlib import Path

from nz_coder import config
from nz_coder.tools import register


class SkillLoader:
    def __init__(self, skills_dir: Path = None):
        self.skills_dir = skills_dir or config.SKILLS_DIR
        self.skills: dict[str, dict] = {}
        self._scan()

    def _scan(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
            meta, body = {}, text
            if m:
                for line in m.group(1).strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                body = m.group(2).strip()
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        return "\n".join(f"- {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            available = ", ".join(self.skills.keys()) or "none"
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# Global instance
skill_loader = SkillLoader()


def _load_skill(name: str) -> str:
    return skill_loader.load(name)


register(
    name="load_skill",
    description="Load specialized domain knowledge by name (e.g. 'code-review').",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name to load."},
        },
        "required": ["name"],
    },
    handler=_load_skill,
)
