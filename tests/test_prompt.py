from __future__ import annotations

from paicli.config import PaiCliConfig
from paicli.prompt import PromptAssembler


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
