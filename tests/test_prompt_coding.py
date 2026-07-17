from __future__ import annotations

from paicli.config import PaiCliConfig
from paicli.prompt import PromptAssembler


def test_prompt_requires_real_workspace_changes_and_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = PromptAssembler(
        config=PaiCliConfig(),
        cwd=str(tmp_path),
        tool_names=["edit_file", "apply_patch", "execute_command"],
        model="test-model",
        provider="test-provider",
    ).build()

    assert "must make the requested workspace changes" in prompt
    assert "`edit_file` or `apply_patch`" in prompt
    assert "change your approach" in prompt
    assert "verify that the requested modifications exist" in prompt
