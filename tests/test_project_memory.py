from __future__ import annotations

from paicli.prompt.project_memory import ProjectMemoryLoader


def test_project_memory_loader_reads_user_project_local_and_imports(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".paicli").mkdir(parents=True)
    (project / ".paicli").mkdir(parents=True)
    (project / "docs").mkdir(parents=True)
    (home / ".paicli" / "PAI.md").write_text("user preference\n", encoding="utf-8")
    (project / "docs" / "rules.md").write_text("imported project rule\n", encoding="utf-8")
    (project / "PAI.md").write_text("@docs/rules.md\nproject rule\n", encoding="utf-8")
    (project / ".paicli" / "PAI.md").write_text("team rule\n", encoding="utf-8")
    (project / "PAI.local.md").write_text("@../outside.md\nlocal rule\n", encoding="utf-8")
    (project / ".paicli" / "PAI.local.md").write_text("private rule\n", encoding="utf-8")

    output = ProjectMemoryLoader(
        user_config_dir=home / ".paicli",
        project_root=project,
    ).load_for_prompt()

    assert "## PAI.md 项目记忆" in output
    assert output.index("user preference") < output.index("imported project rule")
    assert output.index("project rule") < output.index("team rule")
    assert output.index("team rule") < output.index("local rule")
    assert output.index("local rule") < output.index("private rule")
    assert "outside" not in output


def test_project_memory_loader_skips_cycles_and_truncates(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.md").write_text("@b.md\nA\n", encoding="utf-8")
    (project / "b.md").write_text("@a.md\nB\n", encoding="utf-8")
    (project / "PAI.md").write_text("@a.md\n" + ("x" * 25_000), encoding="utf-8")

    output = ProjectMemoryLoader(
        user_config_dir=None,
        project_root=project,
    ).load_for_prompt()

    assert output.splitlines().count("A") == 1
    assert output.splitlines().count("B") == 1
    assert "内容已按 24000 字符预算截断" in output
    assert len(output) < 24_200
