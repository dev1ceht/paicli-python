from __future__ import annotations

import json

from paicli.config import LlmConfig
from paicli.llm import create_llm_client
from paicli.llm.openai_compatible import OpenAICompatibleClient
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
            thinking_level="on",
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
    assert payload["enable_thinking"] is True
    assert "reasoning_effort" not in payload


def test_qwen_auto_with_budget_explicitly_enables_thinking():
    client = create_llm_client(
        LlmConfig(
            provider="custom",
            model="qwen-plus",
            api_key="key",
            thinking_budget=8192,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert payload["enable_thinking"] is True
    assert payload["thinking_budget"] == 8192


def test_qwen_off_disables_thinking_and_suppresses_budget():
    client = create_llm_client(
        LlmConfig(
            provider="qwen",
            model="qwen-plus",
            api_key="key",
            thinking_level="off",
            thinking_budget=8192,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert payload["enable_thinking"] is False
    assert "thinking_budget" not in payload


def test_deepseek_levels_use_thinking_and_reasoning_effort_not_budget():
    client = create_llm_client(
        LlmConfig(
            provider="custom",
            model="deepseek-v4-flash",
            api_key="key",
            thinking_level="high",
            thinking_budget=8192,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "thinking_budget" not in payload
    assert "temperature" not in payload


def test_deepseek_auto_uses_provider_default_thinking_and_removes_sampling():
    client = create_llm_client(
        LlmConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key="key",
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert "thinking" not in payload
    assert "reasoning_effort" not in payload
    assert "thinking_budget" not in payload
    assert "temperature" not in payload


def test_deepseek_off_disables_thinking_and_keeps_sampling():
    client = create_llm_client(
        LlmConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key="key",
            thinking_level="off",
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.7


def test_unknown_model_sends_no_reasoning_fields():
    client = create_llm_client(
        LlmConfig(
            provider="qwen",
            model="custom-model",
            api_key="key",
            thinking_level="high",
            thinking_budget=8192,
        )
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert client.model_name == "custom-model"
    assert "thinking" not in payload
    assert "enable_thinking" not in payload
    assert "reasoning_effort" not in payload
    assert "thinking_budget" not in payload


def test_low_level_client_uses_exact_model_capability():
    client = OpenAICompatibleClient(
        provider_name="qwen",
        model="qwen2.5-coder",
        api_key="key",
        base_url="https://example.test/v1",
        request_format="qwen",
        budget_supported=True,
        thinking_level="high",
        thinking_budget=8192,
    )

    payload = json.loads(
        client.prepare_request(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        ).payload_json
    )

    assert client.thinking_level == "off"
    assert client.request_format == "none"
    assert "enable_thinking" not in payload
    assert "thinking_budget" not in payload


def test_non_qwen_model_does_not_send_thinking_budget_even_with_qwen_provider():
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
