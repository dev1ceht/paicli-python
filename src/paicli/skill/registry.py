from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    path: Path
    content: str


class SkillRegistry:
    """Load SKILL.md files from built-in, user, and project locations."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.user_root = Path.home() / ".paicli" / "skills"
        self.project_skill_root = self.project_root / ".paicli" / "skills"

    def list(self) -> list[Skill]:
        skills: dict[str, Skill] = {}
        for root in [self.user_root, self.project_skill_root]:
            if not root.exists():
                continue
            for skill_file in root.glob("*/SKILL.md"):
                skill = self._load_skill_file(skill_file)
                if skill:
                    skills[skill.name] = skill
        return [skills[name] for name in sorted(skills)]

    def load(self, name: str) -> Skill | None:
        for skill in self.list():
            if skill.name == name:
                return skill
        return None

    def index_text(self, max_chars: int = 4000) -> str:
        skills = self.list()
        if not skills:
            return ""
        lines = ["Available skills:"]
        for skill in skills:
            lines.append(f"- {skill.name}: {skill.description}")
        text = "\n".join(lines)
        return text[:max_chars]

    def _load_skill_file(self, path: Path) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        metadata = _parse_frontmatter(content)
        name = metadata.get("name") or path.parent.name
        description = metadata.get("description") or ""
        return Skill(name=name, description=description, path=path, content=content)


def _parse_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*", content, re.S)
    if not match:
        return {}
    metadata: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata
