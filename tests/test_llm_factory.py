from __future__ import annotations

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


def test_supported_providers_default_to_one_million_token_context_windows():
    deepseek = create_llm_client(LlmConfig(provider="deepseek", model="unlisted-model"))
    openai = create_llm_client(LlmConfig(provider="openai", model="gpt-model"))
    qwen = create_llm_client(LlmConfig(provider="qwen", model="qwen-plus"))

    assert deepseek.max_context_window == 1_000_000
    assert openai.max_context_window == 1_000_000
    assert qwen.max_context_window == 1_000_000


def test_unrecognized_provider_falls_back_to_128k_context_window():
    client = create_llm_client(LlmConfig(provider="custom", model="custom-model"))

    assert client.max_context_window == 128_000


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
