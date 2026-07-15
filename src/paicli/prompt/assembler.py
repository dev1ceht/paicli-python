from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from paicli.config import PaiCliConfig
from paicli.prompt.project_memory import ProjectMemoryLoader
from paicli.skill import SkillRegistry


@dataclass(frozen=True, slots=True)
class PromptSections:
    """Role-stable system prompt sections with explicit reduction seams."""

    prefix: str
    relevant_memory: str = ""
    relevant_memory_entries: tuple[str, ...] = ()
    skills: str = ""
    suffix: str = ""

    def __post_init__(self) -> None:
        if self.relevant_memory and not self.relevant_memory_entries:
            object.__setattr__(
                self,
                "relevant_memory_entries",
                _split_relevant_memory_entries(self.relevant_memory),
            )

    def render(self) -> str:
        return "\n\n".join(
            part.strip()
            for part in (
                self.prefix,
                self._render_relevant_memory(),
                self.skills,
                self.suffix,
            )
            if part and part.strip()
        )

    def drop_least_relevant_memory(self) -> PromptSections:
        if self.relevant_memory_entries:
            remaining = self.relevant_memory_entries[:-1]
            return replace(
                self,
                relevant_memory=self.relevant_memory if remaining else "",
                relevant_memory_entries=remaining,
            )
        return replace(self, relevant_memory="")

    def without_skills(self) -> PromptSections:
        return replace(self, skills="")

    def _render_relevant_memory(self) -> str:
        if not self.relevant_memory_entries:
            return self.relevant_memory
        preamble = _relevant_memory_preamble(self.relevant_memory)
        return "\n".join((*preamble, *self.relevant_memory_entries)).strip()


class PromptAssembler:
    """Assemble role-stable system instructions from versioned Markdown resources."""

    def __init__(
        self,
        config: PaiCliConfig,
        cwd: str,
        tool_names: list[str],
        model: str,
        provider: str,
        tool_summaries: list[tuple[str, str]] | None = None,
    ):
        self.config = config
        self.cwd = str(Path(cwd).resolve())
        self.tool_names = tool_names
        self.model = model
        self.provider = provider
        self.tool_summaries = tool_summaries or [(name, "") for name in tool_names]

    def build(self, *, relevant_memory: str = "") -> str:
        return self.build_sections(relevant_memory=relevant_memory).render()

    def build_sections(self, *, relevant_memory: str = "") -> PromptSections:
        approval = _approval_resource(self.config.policy.hitl_mode)
        prefix = [
            _resource("base.md"),
            _resource("personalities/calm.md"),
            _resource("modes/agent.md"),
            _resource(f"approvals/{approval}.md"),
            _runtime_context(
                cwd=self.cwd,
                model=self.model,
                provider=self.provider,
                tool_summaries=self.tool_summaries,
            ),
            self._project_memory(),
        ]
        suffix = [
            _resource("context/context-management.md"),
            _resource("handoff.md"),
        ]
        return PromptSections(
            prefix=_join_sections(prefix),
            relevant_memory=relevant_memory,
            skills=self._skill_index(),
            suffix=_join_sections(suffix),
        )

    def _project_memory(self) -> str:
        return ProjectMemoryLoader.create_default(self.cwd).load_for_prompt()

    def _skill_index(self) -> str:
        if not self.config.features.skill:
            return ""
        return SkillRegistry(self.cwd).index_text()


def _resource(name: str) -> str:
    return files("paicli.prompt.resources").joinpath(name).read_text(encoding="utf-8")


def _join_sections(parts: list[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _split_relevant_memory_entries(value: str) -> tuple[str, ...]:
    entries: list[list[str]] = []
    for line in value.splitlines():
        if line.lstrip().startswith("- "):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    return tuple("\n".join(lines).rstrip() for lines in entries)


def _relevant_memory_preamble(value: str) -> tuple[str, ...]:
    lines: list[str] = []
    for line in value.splitlines():
        if line.lstrip().startswith("- "):
            break
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    return tuple(lines)


def _approval_resource(hitl_mode: str) -> str:
    return {"never": "never", "auto": "auto"}.get(hitl_mode, "suggest")


def _runtime_context(
    *,
    cwd: str,
    model: str,
    provider: str,
    tool_summaries: list[tuple[str, str]],
) -> str:
    lines = [
        "## 运行时上下文",
        "",
        f"- 当前时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 工作目录：{cwd}",
        f"- 当前模型：{model}（{provider}）",
        "",
        "### 当前可用工具",
    ]
    if not tool_summaries:
        lines.append("- 无")
    else:
        for name, description in tool_summaries:
            suffix = f"：{description}" if description else ""
            lines.append(f"- `{name}`{suffix}")
    return "\n".join(lines)
