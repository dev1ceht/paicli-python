from __future__ import annotations

import json
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()


@dataclass(slots=True)
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0


@dataclass(slots=True)
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    timeout: float = 60.0
    batch_timeout: float = 90.0
    max_concurrent_read: int = 4


@dataclass(slots=True)
class AgentConfig:
    max_turns: int = 20
    max_tool_calls: int = 40
    max_elapsed_seconds: float = 600.0
    max_total_tokens: int = 100_000
    stagnation_threshold: int = 3


@dataclass(slots=True)
class McpConfig:
    servers: list[dict[str, Any]] = field(default_factory=list)
    auto_start: bool = True


@dataclass(slots=True)
class MemoryConfig:
    max_conversation_history: int = 100
    long_term_enabled: bool = True
    long_term_path: str = "~/.paicli/memory/long_term_memory.json"
    long_term_db_path: str = "~/.paicli/memory.db"
    token_budget_mode: str = "balanced"
    compression_threshold: float = 0.8


@dataclass(slots=True)
class PolicyConfig:
    hitl_mode: str = "auto"
    path_guard_enabled: bool = True
    command_blacklist: list[str] = field(
        default_factory=lambda: [
            "sudo",
            "rm -rf /",
            "rm -rf ~",
            "mkfs",
            "dd if=/dev/zero",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "curl | sh",
            "curl|sh",
            "shutdown",
            "reboot",
        ]
    )
    audit_log_path: str = "~/.paicli/audit"
    require_approval_for_writes: bool = False


@dataclass(slots=True)
class PromptConfig:
    personality: str = "default"
    agent_mode: str = "react"
    custom_prompt_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeatureConfig:
    mcp: bool = True
    skill: bool = True
    memory: bool = True
    audit_log: bool = True
    context_compression: bool = True
    code_index: bool = True


@dataclass(slots=True)
class ContextConfig:
    """上下文治理配置"""
    
    # 阶段一：动态预算计算
    utilization_rate: float = 0.50
    output_reserve_tokens: int = 4096
    min_budget_chars: int = 60_000
    max_budget_chars: int = 800_000
    
    # 阶段三：压力感知裁剪
    tier1_threshold: float = 0.60
    tier2_threshold: float = 0.80
    tier3_threshold: float = 0.95
    
    # 阶段四：结构化压缩
    protected_turns: int = 2
    
    # 工具结果裁剪
    tool_result_keep_recent: int = 5
    tool_result_max_total_bytes: int = 204_800  # 200KB
    tool_result_preview_chars: int = 200
    tool_result_storage_dir: str = "~/.paicli/tool_results"


@dataclass(slots=True)
class PaiCliConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    render_mode: str = "inline"
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


def load_config(
    project_root: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None,
) -> PaiCliConfig:
    env_map = env if env is not None else os.environ
    data = _config_to_dict(PaiCliConfig())

    user_config = _read_json(_home() / ".paicli" / "config.json")
    if user_config:
        data = _deep_merge(data, user_config)

    root = Path(project_root).resolve() if project_root else None
    if root:
        project_config = _read_json(root / ".paicli" / "config.json")
        if project_config:
            data = _deep_merge(data, project_config)
        project_env = _read_env(root / ".env")
        if project_env:
            data = _apply_env(data, project_env)

    if overrides:
        data = _deep_merge(data, overrides)

    data = _apply_env(data, env_map)
    config = _dict_to_config(data)
    config.memory.long_term_path = _expand_home(config.memory.long_term_path)
    config.memory.long_term_db_path = _expand_home(config.memory.long_term_db_path)
    config.policy.audit_log_path = _expand_home(config.policy.audit_log_path)
    config.context.tool_result_storage_dir = _expand_home(config.context.tool_result_storage_dir)
    return config


def get_config_paths(project_root: str | Path | None = None) -> list[Path]:
    paths = [_home() / ".paicli" / "config.json"]
    if project_root:
        paths.append(Path(project_root).resolve() / ".paicli" / "config.json")
    return paths


def config_to_public_dict(config: PaiCliConfig) -> dict[str, Any]:
    data = _config_to_dict(config)
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def _apply_env(data: dict[str, Any], env: dict[str, str | None]) -> dict[str, Any]:
    result = deepcopy(data)
    llm = result.setdefault("llm", {})
    features = result.setdefault("features", {})
    policy = result.setdefault("policy", {})

    mappings: list[tuple[str, str, Any]] = [
        ("PAICLI_API_KEY", "api_key", str),
        ("PAICLI_PROVIDER", "provider", str),
        ("PAICLI_MODEL", "model", str),
        ("PAICLI_BASE_URL", "base_url", str),
        ("PAICLI_MAX_TOKENS", "max_tokens", int),
        ("PAICLI_TEMPERATURE", "temperature", float),
    ]
    for env_key, config_key, caster in mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                llm[config_key] = caster(raw)

    provider = str(llm.get("provider") or "").lower()
    if not llm.get("api_key"):
        provider_key_map = {
            "aliyun": [
                "PAICLI_DASHSCOPE_API_KEY",
                "PAICLI_QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
                "QWEN_API_KEY",
            ],
            "bailian": [
                "PAICLI_DASHSCOPE_API_KEY",
                "PAICLI_QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
                "QWEN_API_KEY",
            ],
            "deepseek": "DEEPSEEK_API_KEY",
            "dashscope": [
                "PAICLI_DASHSCOPE_API_KEY",
                "PAICLI_QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
                "QWEN_API_KEY",
            ],
            "glm": "GLM_API_KEY",
            "zhipu": "GLM_API_KEY",
            "step": "STEP_API_KEY",
            "kimi": "KIMI_API_KEY",
            "moonshot": "KIMI_API_KEY",
            "qwen": [
                "PAICLI_QWEN_API_KEY",
                "PAICLI_DASHSCOPE_API_KEY",
                "QWEN_API_KEY",
                "DASHSCOPE_API_KEY",
            ],
            "freellmapi": "FREELLMAPI_API_KEY",
            "xfyun": "XFYUN_API_KEY",
            "agnes": "AGNES_API_KEY",
        }
        prefixed_provider_key = f"PAICLI_{provider.upper()}_API_KEY" if provider else ""
        provider_keys = provider_key_map.get(provider)
        if isinstance(provider_keys, str):
            provider_keys = [provider_keys]
        candidate_keys = []
        if prefixed_provider_key:
            candidate_keys.append(prefixed_provider_key)
        candidate_keys.extend(provider_keys or [])
        for provider_key in candidate_keys:
            if env.get(provider_key):
                llm["api_key"] = env[provider_key]
                break

    provider_model_key = f"{provider.upper()}_MODEL" if provider else ""
    provider_base_url_key = f"{provider.upper()}_BASE_URL" if provider else ""
    prefixed_provider_model_key = f"PAICLI_{provider_model_key}" if provider_model_key else ""
    prefixed_provider_base_url_key = (
        f"PAICLI_{provider_base_url_key}" if provider_base_url_key else ""
    )
    for model_key in [prefixed_provider_model_key, provider_model_key]:
        if model_key and env.get(model_key):
            llm["model"] = env[model_key]
            break
    for base_url_key in [prefixed_provider_base_url_key, provider_base_url_key]:
        if base_url_key and env.get(base_url_key):
            llm["base_url"] = env[base_url_key]
            break

    render_mode = env.get("PAICLI_RENDER_MODE") or env.get("PAICLI_RENDERER")
    if render_mode in {"plain", "inline"}:
        result["render_mode"] = render_mode

    if env.get("PAICLI_TUI") == "true":
        result["render_mode"] = "inline"

    for env_key, feature_key in [
        ("PAICLI_MCP", "mcp"),
        ("PAICLI_SKILL", "skill"),
        ("PAICLI_MEMORY", "memory"),
    ]:
        raw = env.get(env_key)
        if raw == "false":
            features[feature_key] = False
        elif raw == "true":
            features[feature_key] = True

    hitl = env.get("PAICLI_HITL")
    if hitl in {"always", "auto", "never"}:
        policy["hitl_mode"] = hitl

    return result


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in source.items():
        if value is None:
            continue
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _deep_merge(old, value)
        else:
            result[key] = deepcopy(value)
    return result


def _config_to_dict(config: PaiCliConfig) -> dict[str, Any]:
    return asdict(config)


def _dict_to_config(data: dict[str, Any]) -> PaiCliConfig:
    return PaiCliConfig(
        llm=LlmConfig(**data.get("llm", {})),
        render_mode=data.get("render_mode", "inline"),
        tools=ToolsConfig(**data.get("tools", {})),
        agent=AgentConfig(**data.get("agent", {})),
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**data.get("features", {})),
        context=ContextConfig(**data.get("context", {})),
    )


def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())
