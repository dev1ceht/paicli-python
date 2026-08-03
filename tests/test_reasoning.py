from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from paicli.config import LlmConfig, PaiCliConfig, config_to_public_dict, load_config
from paicli.entrypoints.cli import app
from paicli.llm.capabilities import (
    available_thinking_levels,
    get_model_capability,
    resolve_thinking_level,
)
from paicli.session import InteractiveSession, SessionRepository


def test_reasoning_defaults_are_explicit_and_budget_is_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = load_config(project_root=tmp_path, env={})

    assert config.llm.thinking_level is None
    assert config.llm.thinking_budget == 8192
    assert config.llm.max_tokens == 16384


def test_thinking_level_accepts_fixed_env_values_and_rejects_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = load_config(
        project_root=tmp_path,
        env={"PAICLI_THINKING_LEVEL": "high"},
    )

    assert config.llm.thinking_level == "high"

    with pytest.raises(ValueError, match="thinking_level"):
        load_config(project_root=tmp_path, env={"PAICLI_THINKING_LEVEL": "auto"})


def test_thinking_budget_null_disables_budget_in_json_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".paicli").mkdir()
    (tmp_path / ".paicli" / "config.json").write_text(
        json.dumps({"llm": {"thinking_budget": None}}),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path, env={})
    assert config.llm.thinking_budget is None

    config = load_config(
        project_root=tmp_path,
        env={"PAICLI_THINKING_BUDGET": "none"},
    )
    assert config.llm.thinking_budget is None


def test_model_capabilities_are_model_only_and_exact():
    assert get_model_capability("deepseek-v4-flash").request_format == "deepseek"
    assert get_model_capability("qwen-plus").request_format == "qwen"
    assert get_model_capability("qwen2.5-coder").request_format == "none"
    assert available_thinking_levels("deepseek-v4-pro") == ("off", "on", "high", "max")


@pytest.mark.parametrize(
    ("model", "requested", "expected"),
    [
        ("qwen-plus", "medium", "on"),
        ("deepseek-v4-flash", "medium", "high"),
        ("deepseek-v4-pro", "low", "high"),
        ("custom-model", "high", "off"),
    ],
)
def test_resolve_thinking_level_uses_model_capability(
    model: str,
    requested: str,
    expected: str,
):
    assert resolve_thinking_level(model, requested) == expected


def test_unknown_model_defaults_to_off_but_known_models_keep_auto():
    assert resolve_thinking_level("custom-model", None) == "off"
    assert resolve_thinking_level("qwen-plus", None) is None


def test_llm_config_rejects_non_positive_budget_and_unknown_level():
    with pytest.raises(ValueError, match="thinking_budget"):
        LlmConfig(thinking_budget=0)
    with pytest.raises(ValueError, match="thinking_level"):
        LlmConfig(thinking_level="auto")


def test_public_config_shows_level_but_hides_budget():
    public = config_to_public_dict(PaiCliConfig(llm=LlmConfig(thinking_level=None)))

    assert public["llm"]["thinking_level"] == "auto"
    assert "thinking_budget" not in public["llm"]


def test_session_thinking_level_event_round_trips(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    session = InteractiveSession(repository, tmp_path / "workspace")

    assert session.thinking_level_state() == (False, None)
    session.record_thinking_level("high")
    assert session.thinking_level_state() == (True, "high")
    session.record_thinking_level(None)
    assert session.thinking_level_state() == (True, None)


def test_cli_thinking_overrides_environment(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_prompt(prompt, cwd, config):
        captured.update(prompt=prompt, cwd=cwd, config=config)

    monkeypatch.setattr("paicli.entrypoints.cli._run_prompt", fake_run_prompt)
    result = CliRunner().invoke(
        app,
        ["--prompt", "hello", "--cwd", str(tmp_path), "--thinking", "off"],
        env={
            "HOME": str(tmp_path / "home"),
            "PAICLI_API_KEY": "test-key",
            "PAICLI_THINKING_LEVEL": "high",
        },
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].llm.thinking_level == "off"


def test_cli_rejects_auto_as_a_thinking_option(tmp_path):
    result = CliRunner().invoke(
        app,
        ["--prompt", "hello", "--cwd", str(tmp_path), "--thinking", "auto"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 2
    assert "thinking must be one of" in result.output
