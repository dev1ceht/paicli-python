from __future__ import annotations

from pathlib import Path

MAX_TOTAL_CHARS = 24_000
MAX_IMPORT_DEPTH = 3


class ProjectMemoryLoader:
    def __init__(
        self,
        *,
        user_config_dir: str | Path | None,
        project_root: str | Path,
    ):
        self.user_config_dir = (
            Path(user_config_dir).expanduser().resolve() if user_config_dir else None
        )
        self.project_root = Path(project_root).expanduser().resolve()

    @classmethod
    def create_default(cls, project_root: str | Path) -> ProjectMemoryLoader:
        return cls(user_config_dir=Path.home() / ".paicli", project_root=project_root)

    def load_for_prompt(self) -> str:
        body_parts = []
        import_stack: set[Path] = set()
        for source, import_root in self._sources():
            if not source.is_file():
                continue
            content = self._read_with_imports(source, import_root, import_stack, 0).strip()
            if not content:
                continue
            body_parts.append(f"### {source}\n\n{content}")
            joined = "\n\n".join(body_parts)
            if len(joined) >= MAX_TOTAL_CHARS:
                return _truncate_section(joined)
        if not body_parts:
            return ""
        return "## PAI.md 项目记忆\n\n" + "\n\n".join(body_parts)

    def _sources(self) -> list[tuple[Path, Path]]:
        sources: list[tuple[Path, Path]] = []
        if self.user_config_dir:
            sources.append((self.user_config_dir / "PAI.md", self.user_config_dir))
        sources.extend(
            [
                (self.project_root / "PAI.md", self.project_root),
                (self.project_root / ".paicli" / "PAI.md", self.project_root),
                (self.project_root / "PAI.local.md", self.project_root),
                (self.project_root / ".paicli" / "PAI.local.md", self.project_root),
            ]
        )
        return [(path.resolve(), root.resolve()) for path, root in sources]

    def _read_with_imports(
        self,
        file_path: Path,
        import_root: Path,
        import_stack: set[Path],
        depth: int,
    ) -> str:
        normalized = file_path.resolve()
        if depth > MAX_IMPORT_DEPTH:
            return ""
        if not _is_inside(normalized, import_root) or not normalized.is_file():
            return ""
        if normalized in import_stack:
            return ""
        import_stack.add(normalized)
        try:
            lines = []
            for line in normalized.read_text(encoding="utf-8").splitlines():
                if not line.strip().startswith("@"):
                    lines.append(line)
                    continue
                import_path = _parse_import(line)
                if import_path is None:
                    continue
                imported = (normalized.parent / import_path).resolve()
                imported_text = self._read_with_imports(
                    imported,
                    import_root,
                    import_stack,
                    depth + 1,
                ).strip()
                if imported_text:
                    lines.append(imported_text)
            return "\n".join(lines) + "\n"
        except OSError:
            return ""
        finally:
            import_stack.remove(normalized)


def _parse_import(line: str) -> str | None:
    trimmed = line.strip()
    if not trimmed.startswith("@") or len(trimmed) < 2 or " " in trimmed:
        return None
    path = trimmed[1:].strip()
    if Path(path).is_absolute() or ".." in Path(path).parts:
        return None
    return path


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _truncate_section(body: str) -> str:
    keep = max(0, MAX_TOTAL_CHARS - 80)
    truncated = body[:keep].rstrip()
    return (
        "## PAI.md 项目记忆\n\n"
        f"{truncated}\n\n[PAI.md 内容已按 {MAX_TOTAL_CHARS} 字符预算截断]"
    )
