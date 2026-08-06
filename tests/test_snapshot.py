from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from dulwich.objects import Commit
from dulwich.repo import Repo

from paicli.snapshot import SnapshotService


def _run_git(project, *args):
    return subprocess.run(
        ["git", "-C", str(project), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_snapshot_restore_modified_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "note.txt"
    file_path.write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    file_path.write_text("after", encoding="utf-8")

    restored = service.restore(first.id)

    assert restored.id == first.id
    assert file_path.read_text(encoding="utf-8") == "before"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        first.created_at,
    )
    commit = Repo(first.path).get_object(first.id.encode("ascii"))
    assert isinstance(commit, Commit)
    assert commit.author_timezone == 8 * 60 * 60
    assert commit.commit_timezone == 8 * 60 * 60


def test_snapshot_uses_side_git_repository_instead_of_directory_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    (project / "note.txt").write_text("after", encoding="utf-8")
    second = service.create("post-turn")

    assert first.path.name == ".git"
    assert first.path == second.path
    assert first.path.exists()
    assert not (service.root / first.id / "note.txt").exists()


def test_snapshot_restore_removes_files_missing_from_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    (project / "new.txt").write_text("new", encoding="utf-8")
    service.create("post-turn")

    service.restore(first.id)

    assert (project / "note.txt").read_text(encoding="utf-8") == "before"
    assert not (project / "new.txt").exists()


def test_snapshot_restore_recreates_deleted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "note.txt"
    file_path.write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    file_path.unlink()
    service.create("post-turn")

    service.restore(first.id)

    assert file_path.read_text(encoding="utf-8") == "before"


def test_snapshot_excludes_heavy_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "node_modules").mkdir()
    (project / "node_modules" / "pkg.js").write_text("skip", encoding="utf-8")
    (project / "tracked.txt").write_text("keep", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    (project / "tracked.txt").write_text("changed", encoding="utf-8")
    (project / "node_modules" / "pkg.js").write_text("changed but skipped", encoding="utf-8")

    service.restore(first.id)

    assert (project / "tracked.txt").read_text(encoding="utf-8") == "keep"
    assert (
        (project / "node_modules" / "pkg.js").read_text(encoding="utf-8")
        == "changed but skipped"
    )


def test_snapshot_excludes_generated_workspace_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    project = tmp_path / "project"
    project.mkdir()
    generated_files = [
        project / "artifacts" / "result.json",
        project / ".harbor-venv" / "package.py",
        project / ".mypy_cache" / "cache.json",
        project / ".worktrees" / "branch" / "note.txt",
        project / "benchmark" / "runs" / "attempt.json",
    ]
    for path in generated_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    for path in generated_files:
        path.write_text("changed but skipped", encoding="utf-8")

    service.restore(first.id)

    assert all(
        path.read_text(encoding="utf-8") == "changed but skipped"
        for path in generated_files
    )


def test_snapshot_status_reports_side_git_location(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()

    service = SnapshotService(project)
    status = service.status()

    assert "Side-Git" in status
    assert str(service.git_dir) in status


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_checkpoint_uses_hidden_refs_and_restores_untracked_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_SNAPSHOT_BACKEND", "auto")
    project = tmp_path / "project"
    project.mkdir()
    _run_git(project, "init", "-q")
    _run_git(project, "config", "user.name", "PaiCLI Test")
    _run_git(project, "config", "user.email", "snapshot@paicli.local")
    note = project / "note.txt"
    note.write_text("before", encoding="utf-8")
    _run_git(project, "add", "--", "note.txt")
    _run_git(project, "commit", "--no-gpg-sign", "-m", "initial")
    head_before = _run_git(project, "rev-parse", "HEAD").stdout.strip()
    index_before = _run_git(project, "diff", "--cached", "--binary").stdout
    new_file = project / "new.txt"
    new_file.write_text("before-new", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("before-agent-edit")
    assert service.backend == "git"
    assert first.backend == "git"

    note.write_text("after", encoding="utf-8")
    new_file.unlink()

    service.restore(first.id)

    assert note.read_text(encoding="utf-8") == "before"
    assert new_file.read_text(encoding="utf-8") == "before-new"
    assert _run_git(project, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _run_git(project, "diff", "--cached", "--binary").stdout == index_before
    refs = _run_git(project, "for-each-ref", "--format=%(refname)", "refs/paicli/checkpoints")
    assert "refs/paicli/checkpoints/" in refs.stdout
