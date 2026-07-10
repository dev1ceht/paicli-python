from __future__ import annotations

import json

from paicli.config import load_config


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
