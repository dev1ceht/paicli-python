from __future__ import annotations

from paicli.snapshot import SnapshotService


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


def test_snapshot_status_reports_side_git_location(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()

    service = SnapshotService(project)
    status = service.status()

    assert "Side-Git" in status
    assert str(service.git_dir) in status
