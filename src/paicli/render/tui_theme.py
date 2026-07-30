"""Rich semantic colors for PaiCLI's pi-inspired terminal presentation.

Textual owns a separate CSS parser, so ``paicli.tcss`` mirrors this palette.
Tests pin the shared core tokens to make drift between the two channels visible.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TuiTheme:
    page_bg: str
    text: str
    muted: str
    dim: str
    accent: str
    border: str
    border_muted: str
    selected_bg: str
    user_message_bg: str
    tool_pending_bg: str
    tool_success_bg: str
    tool_error_bg: str
    success: str
    error: str
    warning: str
    warning_high: str
    thinking: str
    planning: str


PI_DARK = TuiTheme(
    page_bg="#18181e",
    text="#d4d4d4",
    muted="#808080",
    dim="#666666",
    accent="#8abeb7",
    border="#5f87ff",
    border_muted="#505050",
    selected_bg="#3a3a4a",
    user_message_bg="#343541",
    tool_pending_bg="#282832",
    tool_success_bg="#283228",
    tool_error_bg="#3c2828",
    success="#b5bd68",
    error="#cc6666",
    warning="#ffff00",
    warning_high="#ffaf5f",
    thinking="#808080",
    planning="#b294bb",
)
