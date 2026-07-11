from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

BRANCH_REF = b"refs/heads/main"
SNAPSHOT_IDENT = b"PaiCLI Snapshot <snapshot@paicli.local>"
DEFAULT_EXCLUDES = (
    ".git",
    ".paicli/snapshots",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".idea",
    "*.class",
    "*.jar",
)


@dataclass(slots=True)
class SnapshotRecord:
    id: str
    phase: str
    created_at: str
    path: Path


@dataclass(slots=True)
class _SnapshotConfig:
    enabled: bool
    root: Path
    max_snapshots: int
    excludes: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> _SnapshotConfig:
        configured_excludes = os.getenv("PAICLI_SNAPSHOT_EXCLUDES", "")
        excludes = list(DEFAULT_EXCLUDES)
        for item in configured_excludes.split(","):
            trimmed = item.strip()
            if trimmed and trimmed not in excludes:
                excludes.append(trimmed)
        return cls(
            enabled=_read_bool("PAICLI_SNAPSHOT_ENABLED", True),
            root=Path(os.getenv("PAICLI_SNAPSHOT_DIR", "~/.paicli/snapshots")).expanduser(),
            max_snapshots=max(1, _read_int("PAICLI_SNAPSHOT_MAX", 50)),
            excludes=tuple(excludes),
        )


class SnapshotService:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.config = _SnapshotConfig.from_environment()
        parent = self.project_root.parent if self.project_root.parent else self.project_root
        self.root = (
            self.config.root
            / _hash_key(str(parent))
            / _hash_key(str(self.project_root))
        )
        self.git_dir = self.root / ".git"
        self.skipped_symlink_count = 0
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, phase: str) -> SnapshotRecord:
        if not self.config.enabled:
            return SnapshotRecord(
                id="disabled",
                phase=phase,
                created_at=datetime.now(UTC).isoformat(),
                path=self.git_dir,
            )
        repo = self._open_repo()
        parent = self._head(repo)
        tree_id = self._write_tree(repo, self.project_root)
        now = int(datetime.now(UTC).timestamp())
        commit = Commit()
        commit.tree = tree_id
        commit.parents = [parent] if parent else []
        commit.author = SNAPSHOT_IDENT
        commit.committer = SNAPSHOT_IDENT
        commit.author_time = now
        commit.commit_time = now
        commit.author_timezone = 0
        commit.commit_timezone = 0
        commit.message = f"{phase} {now}".encode()
        repo.object_store.add_object(commit)
        repo.refs.set_symbolic_ref(b"HEAD", BRANCH_REF)
        repo.refs[BRANCH_REF] = commit.id
        return self._record_from_commit(commit)

    def list(self, limit: int = 20) -> list[SnapshotRecord]:
        if not self.config.enabled or not self.git_dir.exists():
            return []
        repo = self._open_repo()
        current = self._head(repo)
        records = []
        max_count = self.config.max_snapshots if limit <= 0 else limit
        while current and len(records) < max_count:
            commit = repo.get_object(current)
            if not isinstance(commit, Commit):
                break
            records.append(self._record_from_commit(commit))
            current = commit.parents[0] if commit.parents else None
        return records

    def restore(self, snapshot_ref: str) -> SnapshotRecord:
        record = self._find_snapshot(snapshot_ref)
        if not record:
            raise ValueError(f"snapshot not found: {snapshot_ref}")
        repo = self._open_repo()
        current = self.create("pre-restore")
        target_commit = repo.get_object(record.id.encode("ascii"))
        current_commit = repo.get_object(current.id.encode("ascii"))
        if not isinstance(target_commit, Commit) or not isinstance(current_commit, Commit):
            raise ValueError(f"snapshot not found: {snapshot_ref}")
        target_tree = self._tree_entries(repo, target_commit.tree)
        current_tree = self._tree_entries(repo, current_commit.tree)
        self._delete_files_missing_from_target(current_tree, target_tree)
        self._write_target_tree(repo, target_tree)
        return record

    def clean(self) -> int:
        count = len(self.list(limit=10_000))
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        return count

    def status(self) -> str:
        latest = self.list(limit=1)
        latest_text = (
            f"{latest[0].phase} {latest[0].id[:10]} {latest[0].created_at}"
            if latest
            else "暂无"
        )
        return "\n".join(
            [
                "Side-Git 快照状态",
                f"状态: {'启用' if self.config.enabled else '关闭'}",
                f"项目根: {self.project_root}",
                f"Side-Git: {self.git_dir}",
                f"最大保留/展示: {self.config.max_snapshots}",
                f"排除: {', '.join(self.config.excludes)}",
                f"最近快照: {latest_text}",
            ]
        )

    def _open_repo(self) -> Repo:
        if not self.git_dir.exists():
            Repo.init_bare(self.git_dir, mkdir=True)
        self._write_exclude_file()
        return Repo(self.git_dir)

    def _head(self, repo: Repo) -> bytes | None:
        refs = repo.get_refs()
        return refs.get(BRANCH_REF) or refs.get(b"HEAD")

    def _write_tree(self, repo: Repo, directory: Path) -> bytes:
        tree = Tree()
        for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
            relative = item.relative_to(self.project_root).as_posix()
            if self._is_excluded(relative):
                continue
            if item.is_symlink():
                self.skipped_symlink_count += 1
                continue
            name = item.name.encode("utf-8")
            if item.is_dir():
                child_id = self._write_tree(repo, item)
                tree.add(name, stat.S_IFDIR, child_id)
            elif item.is_file():
                blob = Blob.from_string(item.read_bytes())
                repo.object_store.add_object(blob)
                tree.add(name, stat.S_IFREG | 0o644, blob.id)
        repo.object_store.add_object(tree)
        return tree.id

    def _tree_entries(
        self,
        repo: Repo,
        tree_id: bytes,
        prefix: str = "",
    ) -> dict[str, bytes]:
        tree = repo.get_object(tree_id)
        if not isinstance(tree, Tree):
            return {}
        entries: dict[str, bytes] = {}
        for entry in tree.items():
            name = entry.path.decode("utf-8")
            relative = f"{prefix}{name}"
            if self._is_excluded(relative):
                continue
            if stat.S_ISDIR(entry.mode):
                entries.update(self._tree_entries(repo, entry.sha, f"{relative}/"))
            else:
                entries[relative] = entry.sha
        return entries

    def _delete_files_missing_from_target(
        self,
        current_tree: dict[str, bytes],
        target_tree: dict[str, bytes],
    ) -> None:
        for relative in current_tree:
            if relative in target_tree or self._is_excluded(relative):
                continue
            target = (self.project_root / relative).resolve()
            if not _is_inside(target, self.project_root) or not target.exists() or target.is_dir():
                continue
            target.unlink()
            self._prune_empty_parents(target.parent)

    def _write_target_tree(self, repo: Repo, target_tree: dict[str, bytes]) -> None:
        for relative, object_id in target_tree.items():
            if self._is_excluded(relative):
                continue
            target = (self.project_root / relative).resolve()
            if not _is_inside(target, self.project_root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            blob = repo.get_object(object_id)
            if isinstance(blob, Blob):
                target.write_bytes(blob.as_raw_string())

    def _prune_empty_parents(self, directory: Path) -> None:
        current = directory
        while _is_inside(current, self.project_root) and current != self.project_root:
            if any(current.iterdir()):
                return
            current.rmdir()
            current = current.parent

    def _find_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None:
        records = self.list(limit=10_000)
        if snapshot_ref.isdigit():
            index = int(snapshot_ref) - 1
            return records[index] if 0 <= index < len(records) else None
        return next((item for item in records if item.id == snapshot_ref), None)

    def _record_from_commit(self, commit: Commit) -> SnapshotRecord:
        message = commit.message.decode("utf-8", errors="replace")
        first_line = message.splitlines()[0] if message else ""
        phase = first_line.split(" ", 1)[0] if first_line else "snapshot"
        return SnapshotRecord(
            id=commit.id.decode("ascii"),
            phase=phase,
            created_at=datetime.fromtimestamp(commit.commit_time, UTC).isoformat(),
            path=self.git_dir,
        )

    def _write_exclude_file(self) -> None:
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        body = "# Managed by PaiCLI side-history snapshots\n" + "\n".join(self.config.excludes)
        (info / "exclude").write_text(body + "\n", encoding="utf-8")

    def _is_excluded(self, relative: str) -> bool:
        normalized = relative.replace("\\", "/").strip("/")
        for raw in self.config.excludes:
            pattern = raw.strip().replace("\\", "/").strip("/")
            if not pattern:
                continue
            if normalized == pattern or normalized.startswith(pattern + "/"):
                return True
            if "*" in pattern and fnmatch.fnmatch(Path(normalized).name, pattern):
                return True
        return False


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_bool(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, fallback: int) -> int:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return int(value.strip())
    except ValueError:
        return fallback


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
