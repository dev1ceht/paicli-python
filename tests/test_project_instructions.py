from __future__ import annotations

from paicli.config import PaiCliConfig
from paicli.prompt import PromptAssembler
from paicli.prompt.project_instructions import ProjectInstructionLoader


def test_loader_reads_global_and_ancestor_files_from_outer_to_current(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    source = repo / "src"
    leaf = source / "feature"
    (home / ".paicli").mkdir(parents=True)
    leaf.mkdir(parents=True)

    (home / ".paicli" / "AGENTS.md").write_text("global instructions", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("outer instructions", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("repo instructions", encoding="utf-8")
    (source / "AGENTS.md").write_text("source instructions", encoding="utf-8")
    (source / "CLAUDE.md").write_text("shadowed source instructions", encoding="utf-8")
    (leaf / "CLAUDE.md").write_text("leaf instructions", encoding="utf-8")

    loader = ProjectInstructionLoader(
        user_config_dir=home / ".paicli",
        cwd=leaf,
    )

    sources = loader.load()

    assert [source.content for source in sources] == [
        "global instructions",
        "outer instructions",
        "repo instructions",
        "source instructions",
        "leaf instructions",
    ]
    assert sources[3].path.name == "AGENTS.md"


def test_loader_falls_back_to_claude_when_agents_candidate_is_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").mkdir()
    (project / "CLAUDE.md").write_text("fallback instructions", encoding="utf-8")

    sources = ProjectInstructionLoader(user_config_dir=None, cwd=project).load()

    assert [source.content for source in sources] == ["fallback instructions"]


def test_loader_reads_full_content_and_treats_at_file_as_plain_text(tmp_path):
    project = tmp_path / "project"
    docs = project / "docs"
    project.mkdir()
    docs.mkdir()
    payload = "x" * 25_000
    (docs / "rules.md").write_text("should not be imported", encoding="utf-8")
    (project / "AGENTS.md").write_text("@docs/rules.md\n" + payload, encoding="utf-8")
    (project / "PAI.md").write_text("old PAI instructions", encoding="utf-8")

    output = ProjectInstructionLoader(user_config_dir=None, cwd=project).load_for_prompt()

    assert "## 项目指令" in output
    assert "@docs/rules.md" in output
    assert payload in output
    assert "should not be imported" not in output
    assert "old PAI instructions" not in output


def test_loader_deduplicates_global_file_when_it_is_also_an_ancestor(tmp_path):
    project = tmp_path / "project"
    leaf = project / "src"
    leaf.mkdir(parents=True)
    context = project / "AGENTS.md"
    context.write_text("shared instructions", encoding="utf-8")

    sources = ProjectInstructionLoader(user_config_dir=project, cwd=leaf).load()

    assert [source.path for source in sources].count(context.resolve()) == 1


def test_prompt_includes_parent_project_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "repo" / "src"
    project.mkdir(parents=True)
    (tmp_path / "repo" / "AGENTS.md").write_text("parent instruction", encoding="utf-8")
    (project / "CLAUDE.md").write_text("current instruction", encoding="utf-8")

    prompt = PromptAssembler(
        config=PaiCliConfig(),
        cwd=str(project),
        tool_names=[],
        model="test-model",
        provider="test-provider",
    ).build()

    assert "## 项目指令" in prompt
    assert "parent instruction" in prompt
    assert "current instruction" in prompt
