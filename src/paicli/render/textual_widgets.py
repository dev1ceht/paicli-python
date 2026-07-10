"""Textual widgets for PaiCLI TUI.

Provides collapsible tool cards, chat log, status bar, and other widgets
that replace the Rich-based rendering with interactive Textual UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Collapsible, Footer, Header, Label, Static, TextArea

# Import shared utilities from _common (single source of truth)
from paicli.render._common import (
    TOOL_LABELS as _TOOL_LABELS,
    format_cost,
    format_elapsed,
    format_payload as _format_payload,
    format_tokens,
    tool_label as _tool_label,
)


def _clip(text: str, limit: int = 1200) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# ToolCard — collapsible widget for tool call results
# ---------------------------------------------------------------------------


class ToolCard(Static):
    """A collapsible card showing a tool call and its result.

    On success the card auto-collapses; on error it stays expanded so the
    user immediately sees the failure output.  Clicking the title bar
    toggles the collapsed state (Textual Collapsible built-in behaviour).
    """

    DEFAULT_CSS = """
    ToolCard {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        background: #14171d;
        border: tall #273244;
    }
    ToolCard .tool-output {
        max-height: 14;
        color: #adb5bd;
        padding: 0 1;
        overflow-x: hidden;
    }
    """

    status: reactive[str] = reactive("running")

    def __init__(
        self,
        tool_name: str,
        args_summary: str = "",
        *,
        task_id: str | None = None,
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args_summary = args_summary[:120]
        self.task_id = task_id
        self._collapsible: Collapsible | None = None
        self._output_widget: Static | None = None

    def _label(self) -> str:
        icon = {"running": "...", "success": "OK", "error": "ERR"}.get(self.status, "..")
        prefix = f"[{self.task_id}] " if self.task_id else ""
        if self.args_summary:
            return f"{prefix}[{icon}] {self.tool_name}: {self.args_summary}"
        return f"{prefix}[{icon}] {self.tool_name}"

    def compose(self) -> ComposeResult:
        self._output_widget = Static("", classes="tool-output")
        self._collapsible = Collapsible(
            self._output_widget,
            title=self._label(),
            collapsed=False,
        )
        yield self._collapsible

    def set_running(self) -> None:
        self.status = "running"
        if self._collapsible:
            self._collapsible.title = self._label()
            self._collapsible.collapsed = False

    def set_success(self, content: str) -> None:
        self.status = "success"
        if self._collapsible and self._output_widget:
            self._output_widget.update(_clip(content))
            self._collapsible.title = self._label()
            self._collapsible.collapsed = True

    def set_error(self, content: str) -> None:
        self.status = "error"
        if self._collapsible and self._output_widget:
            self._output_widget.update(_clip(content))
            self._collapsible.title = self._label()
            self._collapsible.collapsed = False


# ---------------------------------------------------------------------------
# ChatLog — scrollable container for messages and tool cards
# ---------------------------------------------------------------------------


class ChatLog(VerticalScroll):
    """Scrollable chat log that holds assistant messages, user messages,
    and tool cards."""

    can_focus = False

    DEFAULT_CSS = """
    ChatLog {
        width: 100%;
        height: 1fr;
        overflow-y: scroll;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._running_tool_cards: dict[str, ToolCard] = {}
        self._renderable_entries: list[str] = []

    def renderable_text(self) -> str:
        """Return the visible chat text in a stable test-friendly form."""
        return "\n".join(entry for entry in self._renderable_entries if entry)

    def _record_renderable_text(self, text: str) -> None:
        text = str(text or "")
        if text:
            self._renderable_entries.append(text)

    def add_tool_call(self, name: str, args: dict | None = None, *, task_id: str | None = None) -> ToolCard:
        card = ToolCard(
            tool_name=name,
            args_summary=_format_args_summary(name, args),
            task_id=task_id,
        )
        self.mount(card)
        key = f"{task_id or ''}:{name}"
        self._running_tool_cards[key] = card
        self._record_renderable_text(f"{name} {_format_args_summary(name, args)}".strip())
        self.call_after_refresh(self.scroll_end, animate=False)
        return card

    def finish_tool_card(
        self,
        name: str,
        content: str,
        *,
        is_error: bool = False,
        task_id: str | None = None,
    ) -> None:
        key = f"{task_id or ''}:{name}"
        card = self._running_tool_cards.pop(key, None)
        if card is None:
            # Fallback: find the last running card for this tool name
            for k, c in list(self._running_tool_cards.items()):
                if c.tool_name == name:
                    card = c
                    self._running_tool_cards.pop(k, None)
                    break
        if card is None:
            return
        if is_error:
            card.set_error(content)
        else:
            card.set_success(content)
        self._record_renderable_text(content)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_user_message(self, text: str) -> None:
        widget = Static(
            Panel(
                Text(text, style="bold #ffffff"),
                title=Text("\U0001f464 You", style="bold #60a5fa"),
                border_style="#60a5fa",
                expand=True,
            )
        )
        self.mount(widget)
        self._record_renderable_text(f"You: {text}")
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_assistant_text(self, text: str) -> None:
        """Append streaming text to the current assistant message block."""
        widget = Static(
            Panel(
                Markdown(text),
                title=Text("Assistant Output", style="bold #a8ff60"),
                border_style="#3f3f46",
                expand=True,
            )
        )
        self.mount(widget)
        self._record_renderable_text(text)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_thinking(self, text: str) -> None:
        widget = Static(
            Panel(
                Text(text, style="dim"),
                title=Text("\U0001f9e0 \u601d\u8003\u8fc7\u7a0b", style="bold #c084fc"),
                border_style="#6d28d9",
                expand=True,
            )
        )
        self.mount(widget)
        self._record_renderable_text(text)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_info(self, text: str, *, style: str = "dim") -> None:
        widget = Static(Text(text, style=style))
        self.mount(widget)
        self._record_renderable_text(text)
        self.call_after_refresh(self.scroll_end, animate=False)

    def clear_log(self) -> None:
        self.remove_children()
        self._running_tool_cards.clear()
        self._renderable_entries.clear()


# ---------------------------------------------------------------------------
# StatusBar — bottom status bar showing model, tokens, cost, etc.
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Bottom status bar showing model info, token usage, cost, and phase."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #000000;
        color: #ffffff;
        padding: 0 1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    model: reactive[str] = reactive("")
    phase: reactive[str] = reactive("idle")
    context_text: reactive[str] = reactive("ctx 0%")
    token_detail: reactive[str] = reactive("")
    cost_text: reactive[str] = reactive("")
    elapsed_text: reactive[str] = reactive("")

    def render(self) -> str:
        parts: list[str] = []
        # Phase
        phase_icon = {"idle": "\u25cb", "running": "\u25cf", "plan": "\U0001f4cb"}.get(
            self.phase, "\u25cb"
        )
        parts.append(f"[bold #a3e635]{phase_icon} {self.phase}[/bold #a3e635]")
        # Model
        if self.model:
            parts.append(f"  [bold]{self.model}[/bold]")
        # Context
        parts.append(f"  {self.context_text}")
        # Token detail
        if self.token_detail:
            parts.append(f"  [dim]{self.token_detail}[/dim]")
        # Cost
        if self.cost_text:
            parts.append(f"  [bold #f97316]{self.cost_text}[/bold #f97316]")
        # Elapsed
        if self.elapsed_text:
            parts.append(f"  [dim]{self.elapsed_text}[/dim]")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# InputBar — user input field at the bottom
# ---------------------------------------------------------------------------


class InputBar(Horizontal):
    """Input bar with prompt indicator and text input."""

    DEFAULT_CSS = """
    InputBar {
        height: 3;
        background: #000000;
        padding: 0 1;
    }
    InputBar TextArea {
        width: 1fr;
        height: 1;
    }
    InputBar Label {
        width: auto;
        content-align: left middle;
        color: #ffffff;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield Label("* ")
        yield CommandInput(placeholder="Type your message or /command", compact=True)


class CommandInput(TextArea):
    """TextArea that owns chat submission while it has focus."""

    BINDINGS = [
        Binding("enter", "submit_message", "Send", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "New line", show=False, priority=True),
    ]

    def action_submit_message(self) -> None:
        """Delegate Enter to the app only from the focused command input."""
        self.app.action_submit_message()

    def action_insert_newline(self) -> None:
        """Keep Shift+Enter available for a multiline draft."""
        self.insert("\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_args_summary(name: str, args: dict | None) -> str:
    """Build a human-readable summary for the collapsible title."""
    if not args:
        return ""
    info = _TOOL_LABELS.get(name)
    if info:
        _, key_param = info
        if key_param and key_param in args:
            return str(args[key_param])[:80]
    if name.startswith("mcp__"):
        return ""
    # Fallback: show first arg value
    for v in args.values():
        return str(v)[:80]
    return ""


# format_tokens, format_elapsed, format_cost are re-exported from _common above
