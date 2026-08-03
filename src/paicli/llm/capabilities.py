from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from paicli.thinking import THINKING_LEVEL_SET, THINKING_LEVELS

RequestFormat = Literal["qwen", "deepseek", "none"]


@dataclass(frozen=True, slots=True)
class ModelCapability:
    supported_levels: tuple[str, ...]
    budget_supported: bool
    request_format: RequestFormat

    @property
    def reasoning_supported(self) -> bool:
        return self.request_format != "none"

    @property
    def supports_reasoning_content(self) -> bool:
        return self.request_format == "deepseek"


_DEEPSEEK_FLASH = ModelCapability(
    supported_levels=("off", "on", "low", "high", "max"),
    budget_supported=False,
    request_format="deepseek",
)
_DEEPSEEK_PRO = ModelCapability(
    supported_levels=("off", "on", "high", "max"),
    budget_supported=False,
    request_format="deepseek",
)
_QWEN = ModelCapability(
    supported_levels=("off", "on"),
    budget_supported=True,
    request_format="qwen",
)
_UNKNOWN = ModelCapability(
    supported_levels=("off",),
    budget_supported=False,
    request_format="none",
)

MODEL_CAPABILITIES: Final = MappingProxyType(
    {
        "deepseek-v4-flash": _DEEPSEEK_FLASH,
        "deepseek-v4-pro": _DEEPSEEK_PRO,
        "qwen3.7-plus": _QWEN,
        "qwen3.7-max": _QWEN,
        "qwen3.6-flash": _QWEN,
        "qwen-plus": _QWEN,
        "qwen-turbo": _QWEN,
    }
)

_LEVEL_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_DEEPSEEK_CLAMPS: Final[dict[str, dict[str, str]]] = {
    "deepseek-v4-flash": {
        "minimal": "low",
        "medium": "high",
        "xhigh": "high",
    },
    "deepseek-v4-pro": {
        "minimal": "high",
        "low": "high",
        "medium": "high",
        "xhigh": "max",
    },
}


def get_model_capability(model: str) -> ModelCapability:
    return MODEL_CAPABILITIES.get(model, _UNKNOWN)


def available_thinking_levels(model: str) -> tuple[str, ...]:
    return get_model_capability(model).supported_levels


def resolve_thinking_level(model: str, requested: str | None) -> str | None:
    if requested is not None and requested not in THINKING_LEVEL_SET:
        values = ", ".join(THINKING_LEVELS)
        raise ValueError(f"thinking_level must be one of: {values}")

    capability = get_model_capability(model)
    if not capability.reasoning_supported:
        return "off"
    if requested is None:
        return None
    if requested == "off":
        return "off"
    if capability.request_format == "qwen":
        return "on"
    if requested in capability.supported_levels:
        return requested

    mapped = _DEEPSEEK_CLAMPS.get(model, {}).get(requested)
    if mapped is not None:
        return mapped

    requested_index = _LEVEL_ORDER.index(requested)
    for candidate in _LEVEL_ORDER[requested_index:]:
        if candidate in capability.supported_levels:
            return candidate
    for candidate in reversed(_LEVEL_ORDER[:requested_index]):
        if candidate in capability.supported_levels:
            return candidate
    return "off"
