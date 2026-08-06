from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from paicli.clock import EAST_EIGHT, format_timestamp, now_timestamp
from paicli.snapshot.checkpoint import SnapshotRecord

CHECKPOINT_REF_PREFIX = "refs/paicli/checkpoints/"
SNAPSHOT_IDENT = ("PaiCLI Snapshot", "snapshot@paicli.local")


def find_git_root(project_root: str | Path) -> Path | None:
    """Return the Git root only when the supplied directory is inside Git."""

    git = shutil.which("git")
    if not git:
        return None
    root = Path(project_root).resolve()
    result = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


class GitCheckpointAdapter:
    """Store workspace checkpoints as hidden refs in the project's Git repository."""

    backend = "git"

    def __init__(
        self,
        project_root: str | Path,
        *,
        enabled: bool,
        max_snapshots: int,
        excludes: tuple[str, ...],
    ):
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root
        self.enabled = enabled
        self.max_snapshots = max(1, max_snapshots)
        self.excludes = excludes
        self.git = shutil.which("git")
        if not self.git:
            raise RuntimeError("Git executable was not found")
        git_dir = self._git_output(["rev-parse", "--git-dir"])
        self.git_dir = Path(git_dir)
        if not self.git_dir.is_absolute():
            self.git_dir = (self.project_root / self.git_dir).resolve()

    def create(self, phase: str) -> SnapshotRecord:
        if not self.enabled:
            return SnapshotRecord(
                id="disabled",
                phase=phase,
                created_at=now_timestamp(),
                path=self.git_dir,
                backend=self.backend,
            )
        timestamp = datetime.now(EAST_EIGHT)
        tree_id = self._capture_tree()
        parent = self._head()
        commit_id = self._commit_tree(tree_id, phase, timestamp, parent)
        self._git(["update-ref", f"{CHECKPOINT_REF_PREFIX}{commit_id}", commit_id])
        self._prune()
        return SnapshotRecord(
            id=commit_id,
            phase=_phase_name(phase),
            created_at=format_timestamp(timestamp),
            path=self.git_dir,
            backend=self.backend,
        )

    def list(self, limit: int = 20) -> list[SnapshotRecord]:
        if not self.enabled:
            return []
        output = self._git_output(
            [
                "for-each-ref",
                "--sort=-creatordate",
                "--format=%(objectname)\t%(creatordate:unix)\t%(subject)",
                CHECKPOINT_REF_PREFIX,
            ]
        )
        records: list[SnapshotRecord] = []
        max_count = self.max_snapshots if limit <= 0 else limit
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            commit_id, timestamp_text, subject = parts
            if not commit_id:
                continue
            try:
                created_at = format_timestamp(
                    datetime.fromtimestamp(int(timestamp_text), EAST_EIGHT)
                )
            except (TypeError, ValueError, OverflowError):
                created_at = now_timestamp()
            records.append(
                SnapshotRecord(
                    id=commit_id,
                    phase=_phase_name(subject),
                    created_at=created_at,
                    path=self.git_dir,
                    backend=self.backend,
                )
            )
            if len(records) >= max_count:
                break
        return records

    def restore(self, snapshot_ref: str) -> SnapshotRecord:
        record = self._find_snapshot(snapshot_ref)
        if record is None:
            raise ValueError(f"snapshot not found: {snapshot_ref}")
        current = self.create("pre-restore")
        deleted = self._git_output(
            ["diff", "--name-only", "--diff-filter=D", current.id, record.id]
        )
        for relative in deleted.splitlines():
            self._delete_path(relative)
        with self._temporary_index() as env:
            self._git(["read-tree", record.id], env=env)
            prefix = str(self.project_root).replace("\\", "/").rstrip("/") + "/"
            self._git(
                ["checkout-index", "--all", "--force", f"--prefix={prefix}"],
                env=env,
            )
        return record

    def clean(self) -> int:
        records = self.list(limit=10_000)
        for record in records:
            self._git(["update-ref", "-d", f"{CHECKPOINT_REF_PREFIX}{record.id}"])
        return len(records)

    def status(self) -> str:
        latest = self.list(limit=1)
        latest_text = (
            f"{latest[0].phase} {latest[0].id[:10]} {latest[0].created_at}"
            if latest
            else "暂无"
        )
        return "\n".join(
            [
                "Git 工作区 checkpoint",
                f"状态: {'启用' if self.enabled else '关闭'}",
                f"项目根: {self.project_root}",
                f"Git 目录: {self.git_dir}",
                f"最大保留/展示: {self.max_snapshots}",
                f"排除: {', '.join(self.excludes)}",
                f"最近 checkpoint: {latest_text}",
            ]
        )

    def _capture_tree(self) -> str:
        with self._temporary_index() as env:
            head = self._head()
            if head:
                self._git(["read-tree", head], env=env)
            self._git(["add", "-A", "--", "."], env=env)
            self._remove_excluded_entries(env)
            return self._git_output(["write-tree"], env=env)

    def _commit_tree(
        self,
        tree_id: str,
        phase: str,
        timestamp: datetime,
        parent: str | None,
    ) -> str:
        message = f"{_phase_name(phase)} {int(timestamp.timestamp())}\n"
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": SNAPSHOT_IDENT[0],
                "GIT_AUTHOR_EMAIL": SNAPSHOT_IDENT[1],
                "GIT_COMMITTER_NAME": SNAPSHOT_IDENT[0],
                "GIT_COMMITTER_EMAIL": SNAPSHOT_IDENT[1],
            }
        )
        args = ["commit-tree", tree_id]
        if parent:
            args.extend(["-p", parent])
        return self._git_output(args, env=env, input_text=message).strip()

    def _prune(self) -> None:
        records = self.list(limit=10_000)
        for record in records[self.max_snapshots :]:
            self._git(["update-ref", "-d", f"{CHECKPOINT_REF_PREFIX}{record.id}"])

    def _remove_excluded_entries(self, env: dict[str, str]) -> None:
        output = self._git(["ls-files", "-z"], env=env, text=False).stdout
        for raw_path in output.split(b"\0"):
            if not raw_path:
                continue
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            if self._is_excluded(relative):
                self._git(["update-index", "--force-remove", "--", relative], env=env)

    def _delete_path(self, relative: str) -> None:
        normalized = relative.replace("\\", "/").strip("/")
        if self._is_excluded(normalized):
            return
        target = (self.project_root / normalized).resolve()
        if not _is_inside(target, self.project_root):
            return
        if target.is_symlink() or target.is_file():
            target.unlink()
            self._prune_empty_parents(target.parent)
        elif target.is_dir():
            shutil.rmtree(target)

    def _prune_empty_parents(self, directory: Path) -> None:
        current = directory
        while _is_inside(current, self.project_root) and current != self.project_root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _find_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None:
        records = self.list(limit=10_000)
        if snapshot_ref.isdigit():
            index = int(snapshot_ref) - 1
            return records[index] if 0 <= index < len(records) else None
        return next(
            (
                record
                for record in records
                if record.id == snapshot_ref or record.id.startswith(snapshot_ref)
            ),
            None,
        )

    def _head(self) -> str | None:
        result = self._git(
            ["rev-parse", "--verify", "HEAD"],
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    @contextmanager
    def _temporary_index(self) -> Iterator[dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="paicli-checkpoint-") as directory:
            index_path = Path(directory) / "index"
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(index_path)
            yield env

    def _git_output(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> str:
        result = self._git(args, env=env, input_text=input_text)
        return result.stdout.strip()

    def _git(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [self.git, "-C", str(self.project_root), *args],
            input=input_text,
            capture_output=True,
            text=text,
            env=env,
            check=False,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if not text else result.stderr
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr.strip()}")
        return result

    def _is_excluded(self, relative: str) -> bool:
        normalized = relative.replace("\\", "/").strip("/")
        for raw in self.excludes:
            pattern = raw.strip().replace("\\", "/").strip("/")
            if not pattern:
                continue
            if normalized == pattern or normalized.startswith(pattern + "/"):
                return True
            if "*" in pattern and fnmatch.fnmatch(Path(normalized).name, pattern):
                return True
        return False


def _phase_name(value: str) -> str:
    normalized = " ".join(str(value).split())
    return (normalized.split(" ", 1)[0] if normalized else "snapshot")[:80]


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
