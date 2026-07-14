from __future__ import annotations

import json
from io import StringIO

import pytest
from rich.console import Console

from paicli.config import PaiCliConfig, load_config, load_llm_config_for_provider
from paicli.entrypoints.repl import _model_command


def test_config_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".paicli").mkdir(parents=True)
    (project / ".paicli").mkdir(parents=True)
    (home / ".paicli" / "config.json").write_text(
        json.dumps({"llm": {"provider": "home", "model": "home-model"}}),
        encoding="utf-8",
    )
    (project / ".paicli" / "config.json").write_text(
        json.dumps({"llm": {"provider": "project", "model": "project-model"}}),
        encoding="utf-8",
    )
    (project / ".env").write_text("PAICLI_MODEL=env-file-model\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PAICLI_PROVIDER", "process")

    config = load_config(
        project_root=project,
        overrides={"llm": {"model": "cli-model"}},
    )

    assert config.llm.provider == "process"
    assert config.llm.model == "cli-model"


def test_provider_specific_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "deepseek")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "deepseek-key"


def test_qwen_provider_uses_dashscope_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "qwen")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "dashscope-key"


def test_qwen_provider_prefers_qwen_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "qwen")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "qwen-key"


def test_paicli_prefixed_qwen_env_overrides_provider_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "qwen")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.setenv("PAICLI_QWEN_MODEL", "qwen-max3.7")
    monkeypatch.setenv(
        "PAICLI_QWEN_BASE_URL",
        "https://example.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("PAICLI_QWEN_API_KEY", "paicli-qwen-key")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.provider == "qwen"
    assert config.llm.model == "qwen-max3.7"
    assert config.llm.base_url == "https://example.aliyuncs.com/compatible-mode/v1"
    assert config.llm.api_key == "paicli-qwen-key"


def test_paicli_prefixed_deepseek_api_key_is_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "deepseek")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("PAICLI_DEEPSEEK_API_KEY", "paicli-deepseek-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "paicli-deepseek-key"


def test_provider_switch_resolves_target_provider_settings_from_project_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ["PAICLI_API_KEY", "PAICLI_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PAICLI_PROVIDER=deepseek",
                "PAICLI_DEEPSEEK_API_KEY=deepseek-key",
                "PAICLI_QWEN_API_KEY=qwen-key",
                "PAICLI_QWEN_BASE_URL=https://qwen.example/v1",
            ]
        ),
        encoding="utf-8",
    )

    llm = load_llm_config_for_provider(tmp_path, "qwen", "qwen-turbo")

    assert llm.provider == "qwen"
    assert llm.model == "qwen-turbo"
    assert llm.api_key == "qwen-key"
    assert llm.base_url == "https://qwen.example/v1"


def test_legacy_repl_model_command_switches_the_active_agent(tmp_path, monkeypatch):
    class SwitchingAgent:
        def __init__(self, config):
            self.config = config
            self.configured = None

        def reconfigure_llm(self, llm_config):
            self.configured = llm_config
            self.config.llm = llm_config
            return type(
                "Client",
                (),
                {"model_name": llm_config.model, "provider_name": llm_config.provider},
            )()

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".env").write_text(
        "PAICLI_QWEN_API_KEY=qwen-key\n",
        encoding="utf-8",
    )
    config = PaiCliConfig()
    agent = SwitchingAgent(config)

    _model_command("qwen qwen-turbo", Console(file=StringIO()), str(tmp_path), config, agent)

    assert agent.configured is not None
    assert config.llm.provider == "qwen"
    assert config.llm.model == "qwen-turbo"


def test_retry_and_replan_settings_have_defaults_and_per_scope_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = load_config(
        project_root=tmp_path,
        overrides={
            "retry": {
                "default": {"max_retries": 2, "base_delay": 0.5},
                "llm": {"max_delay": 4.0},
                "tools": {"enabled": False},
            },
            "plan": {"replan_progress_threshold": 0.4, "max_replans": 1},
        },
    )

    llm_retry = config.retry.resolve("llm")
    tool_retry = config.retry.resolve("tools")
    assert llm_retry.max_retries == 2
    assert llm_retry.base_delay == 0.5
    assert llm_retry.max_delay == 4.0
    assert tool_retry.enabled is False
    assert config.plan.replan_progress_threshold == 0.4
    assert config.plan.max_replans == 1


def test_replan_configuration_rejects_more_than_one_automatic_replan(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with pytest.raises(ValueError, match="max_replans"):
        load_config(project_root=tmp_path, overrides={"plan": {"max_replans": 2}})
