from __future__ import annotations

from datetime import datetime
from importlib.resources import files
from pathlib import Path

from paicli.config import PaiCliConfig
from paicli.prompt.project_memory import ProjectMemoryLoader
from paicli.skill import SkillRegistry


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
        approval = _approval_resource(self.config.policy.hitl_mode)
        parts = [
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
            relevant_memory,
            self._skill_index(),
            _resource("context/context-management.md"),
            _resource("handoff.md"),
        ]
        return "\n\n".join(part.strip() for part in parts if part and part.strip())

    def _project_memory(self) -> str:
        return ProjectMemoryLoader.create_default(self.cwd).load_for_prompt()

    def _skill_index(self) -> str:
        if not self.config.features.skill:
            return ""
        return SkillRegistry(self.cwd).index_text()


def _resource(name: str) -> str:
    return files("paicli.prompt.resources").joinpath(name).read_text(encoding="utf-8")


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
