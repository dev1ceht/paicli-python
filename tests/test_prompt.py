from __future__ import annotations

from paicli.config import PaiCliConfig
from paicli.prompt import PromptAssembler


def test_prompt_sections_preserve_rendering_and_allow_entry_aware_reduction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assembler = PromptAssembler(
        config=PaiCliConfig(),
        cwd=str(tmp_path),
        tool_names=[],
        model="test-model",
        provider="test-provider",
    )
    sections = assembler.build_sections(
        relevant_memory="## Relevant memory\n\n- highest\n- lowest\n"
    )

    assert "- highest" in sections.render()
    assert "- lowest" not in sections.drop_least_relevant_memory().render()
    assert "- highest" in sections.drop_least_relevant_memory().render()
    assert sections.without_skills().skills == ""


def test_prompt_reduction_removes_a_complete_multiline_memory_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sections = PromptAssembler(
        config=PaiCliConfig(),
        cwd=str(tmp_path),
        tool_names=[],
        model="test-model",
        provider="test-provider",
    ).build_sections(
        relevant_memory=(
            "## Relevant memory\n\n"
            "- highest\n  highest continuation\n"
            "- lowest\n  lowest continuation\n"
        )
    )

    reduced = sections.drop_least_relevant_memory().render()

    assert "highest continuation" in reduced
    assert "lowest" not in reduced
    assert "lowest continuation" not in reduced


def test_prompt_uses_layered_resources_and_runtime_tool_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = PromptAssembler(
        config=PaiCliConfig(),
        cwd=str(tmp_path),
        tool_names=["inspect_workspace"],
        tool_summaries=[("inspect_workspace", "检查工作区状态")],
        model="test-model",
        provider="test-provider",
    ).build(relevant_memory="## 相关长期记忆\n\n使用 pytest 验证")

    assert "## 身份" in prompt
    assert "默认使用中文回复" in prompt
    assert "## 模式：ReAct Agent" in prompt
    assert "## 审批" in prompt
    assert "## 运行时上下文" in prompt
    assert "`inspect_workspace`：检查工作区状态" in prompt
    assert "## 相关长期记忆" in prompt
    assert "## 上下文管理" in prompt
    assert "## 最终回复" in prompt
