from __future__ import annotations

import json

from paicli.config import LlmConfig
from paicli.llm import create_llm_client
from paicli.types import Message


def test_qwen_provider_uses_dashscope_openai_compatible_base_url():
    client = create_llm_client(LlmConfig(provider="qwen", model="qwen-plus", api_key="key"))

    assert client.provider_name == "qwen"
    assert client.model_name == "qwen-plus"
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_dashscope_provider_alias_uses_same_base_url():
    client = create_llm_client(LlmConfig(provider="dashscope", model="qwen-plus", api_key="key"))

    assert client.provider_name == "dashscope"
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_qwen_request_limits_thinking_to_configured_budget():
    client = create_llm_client(
        LlmConfig(
            provider="qwen",
            model="qwen3.7-plus",
            api_key="key",
            thinking_budget=4096,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert payload["thinking_budget"] == 4096


def test_non_qwen_request_does_not_send_provider_specific_thinking_budget():
    client = create_llm_client(
        LlmConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="key",
            thinking_budget=4096,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert "thinking_budget" not in payload


def test_known_model_uses_its_reported_context_window():
    client = create_llm_client(LlmConfig(provider="deepseek", model="deepseek-v4-flash"))

    assert client.max_context_window == 1_000_000
    assert client.reported_context_window == 1_000_000


def test_current_qwen_models_use_their_official_one_million_token_window():
    for model in ("qwen3.7-plus", "qwen3.7-max", "qwen3.6-flash"):
        client = create_llm_client(LlmConfig(provider="qwen", model=model))

        assert client.max_context_window == 1_000_000
        assert client.reported_context_window == 1_000_000


def test_unknown_model_uses_an_unreported_128k_safety_window():
    client = create_llm_client(LlmConfig(provider="openai", model="custom-model"))

    assert client.max_context_window == 128_000
    assert client.reported_context_window is None


def test_user_context_window_override_is_reported_and_used_for_safety():
    client = create_llm_client(
        LlmConfig(
            provider="openai",
            model="custom-model",
            context_window=64_000,
        )
    )

    assert client.max_context_window == 64_000
    assert client.reported_context_window == 64_000


def test_deepseek_replays_reasoning_content_but_other_providers_do_not():
    message = Message(
        role="assistant",
        content="",
        reasoning_content="inspect the file before changing it",
    )
    deepseek = create_llm_client(LlmConfig(provider="deepseek", model="deepseek-v4-flash"))
    qwen = create_llm_client(LlmConfig(provider="qwen", model="qwen-plus"))

    deepseek_message = deepseek._format_messages([message], "system")[1]
    qwen_message = qwen._format_messages([message], "system")[1]

    assert deepseek_message["reasoning_content"] == "inspect the file before changing it"
    assert "reasoning_content" not in qwen_message
