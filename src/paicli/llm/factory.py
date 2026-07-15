from __future__ import annotations

from paicli.config import LlmConfig
from paicli.llm.openai_compatible import OpenAICompatibleClient
from paicli.retry import RetryPolicy

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
PROVIDER_BASE_URLS = {
    "aliyun": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "bailian": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "step": "https://api.stepfun.com/v1",
}

MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-coder": 128_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.7-max": 1_000_000,
    "qwen3.6-flash": 1_000_000,
}
UNKNOWN_MODEL_SAFETY_WINDOW = 128_000


def create_llm_client(
    config: LlmConfig,
    *,
    retry_policy: RetryPolicy | None = None,
    retry_audit_path: str | None = None,
    retry_cwd: str = "",
) -> OpenAICompatibleClient:
    provider = config.provider.lower()
    retry = retry_policy or RetryPolicy()
    audit_path = retry_audit_path or "~/.paicli/audit"
    reported_context = config.context_window or MODEL_CONTEXT_WINDOWS.get(config.model)
    context = reported_context or UNKNOWN_MODEL_SAFETY_WINDOW
    context_known = reported_context is not None
    if provider == "deepseek":
        base_url = config.base_url or DEEPSEEK_BASE_URL
        return OpenAICompatibleClient(
            provider_name="deepseek",
            model=config.model,
            api_key=config.api_key,
            base_url=base_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=context,
            context_window_known=context_known,
            prompt_cache=True,
            supports_reasoning_content=True,
            retry_policy=retry,
            retry_audit_path=audit_path,
            retry_cwd=retry_cwd,
        )
    if provider in {"openai", "openai-compatible", "compatible"}:
        return OpenAICompatibleClient(
            provider_name=provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or OPENAI_BASE_URL,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=context,
            context_window_known=context_known,
            prompt_cache=False,
            retry_policy=retry,
            retry_audit_path=audit_path,
            retry_cwd=retry_cwd,
        )
    if provider in PROVIDER_BASE_URLS:
        return OpenAICompatibleClient(
            provider_name=provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or PROVIDER_BASE_URLS[provider],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=context,
            context_window_known=context_known,
            prompt_cache=False,
            retry_policy=retry,
            retry_audit_path=audit_path,
            retry_cwd=retry_cwd,
        )
    return OpenAICompatibleClient(
        provider_name=provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or DEEPSEEK_BASE_URL,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout,
        max_context_window=context,
        context_window_known=context_known,
        prompt_cache=False,
        retry_policy=retry,
        retry_audit_path=audit_path,
        retry_cwd=retry_cwd,
    )
