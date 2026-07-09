from __future__ import annotations

from paicli.config import LlmConfig
from paicli.llm import create_llm_client


def test_qwen_provider_uses_dashscope_openai_compatible_base_url():
    client = create_llm_client(
        LlmConfig(provider="qwen", model="qwen-plus", api_key="key")
    )

    assert client.provider_name == "qwen"
    assert client.model_name == "qwen-plus"
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_dashscope_provider_alias_uses_same_base_url():
    client = create_llm_client(
        LlmConfig(provider="dashscope", model="qwen-plus", api_key="key")
    )

    assert client.provider_name == "dashscope"
    assert client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
