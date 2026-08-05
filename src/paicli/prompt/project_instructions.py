from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CONTEXT_FILE_NAMES = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


@dataclass(frozen=True, slots=True)
class InstructionSource:
    """One fully loaded project instruction file."""

    path: Path
    content: str


class ProjectInstructionLoader:
    """Discover and render Pi-compatible AGENTS.md/CLAUDE.md instructions."""

    def __init__(
        self,
        *,
        user_config_dir: str | Path | None,
        cwd: str | Path,
    ):
        self.user_config_dir = (
            Path(user_config_dir).expanduser().resolve() if user_config_dir else None
        )
        self.cwd = Path(cwd).expanduser().resolve()

    @classmethod
    def create_default(cls, cwd: str | Path) -> ProjectInstructionLoader:
        return cls(user_config_dir=Path.home() / ".paicli", cwd=cwd)

    def load(self) -> tuple[InstructionSource, ...]:
        """Return global and ancestor instructions in least-to-most-specific order."""
        sources: list[InstructionSource] = []
        seen_paths: set[Path] = set()

        self._append_unique(sources, seen_paths, self._load_from_dir(self.user_config_dir))

        current = self.cwd.parent if self.cwd.is_file() else self.cwd
        ancestor_sources: list[InstructionSource] = []
        while True:
            self._append_unique(
                ancestor_sources,
                seen_paths,
                self._load_from_dir(current),
            )
            parent = current.parent
            if parent == current:
                break
            current = parent

        sources.extend(reversed(ancestor_sources))
        return tuple(sources)

    def load_for_prompt(self) -> str:
        """Render every discovered file without parsing or truncating its content."""
        sources = self.load()
        if not sources:
            return ""

        sections = [f"### {source.path}\n\n{source.content}" for source in sources]
        return "## 项目指令\n\n" + "\n\n".join(sections)

    @staticmethod
    def _append_unique(
        target: list[InstructionSource],
        seen_paths: set[Path],
        source: InstructionSource | None,
    ) -> None:
        if source is None or source.path in seen_paths:
            return
        seen_paths.add(source.path)
        target.append(source)

    @staticmethod
    def _load_from_dir(directory: Path | None) -> InstructionSource | None:
        if directory is None:
            return None

        for filename in CONTEXT_FILE_NAMES:
            candidate = directory / filename
            try:
                if not candidate.is_file():
                    continue
                path = candidate.resolve()
                return InstructionSource(
                    path=path,
                    content=path.read_text(encoding="utf-8"),
                )
            except (OSError, RuntimeError, UnicodeError) as exc:
                logger.warning("Could not read project instruction file %s: %s", candidate, exc)

        return None
