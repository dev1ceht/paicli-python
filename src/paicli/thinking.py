from __future__ import annotations

from typing import Final, Literal

ThinkingLevel = Literal[
    "off",
    "on",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]

THINKING_LEVELS: Final[tuple[str, ...]] = (
    "off",
    "on",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
THINKING_LEVEL_SET: Final[frozenset[str]] = frozenset(THINKING_LEVELS)
