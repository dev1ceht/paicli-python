"""Textual widgets for PaiCLI TUI.

Provides collapsible tool cards, chat log, status bar, and other widgets
that replace the Rich-based rendering with interactive Textual UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rich.errors import MarkupError, StyleSyntaxError
from rich.markdown import Markdown
from rich.panel import Panel
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Collapsible, Label, Static, TextArea

# Import shared utilities from _common (single source of truth)
from paicli.render._common import (
    TOOL_LABELS as _TOOL_LABELS,
    format_cost,
    format_elapsed,
    format_tokens,
)
from paicli.render.history import PromptHistory


class MessageBlock(Static):
    """Mounted message block with test-visible content."""

    def __init__(self, role: str, text: str = "", *, task_id: str | None = None) -> None:
        super().__init__()
        self.role = role
        self.task_id = task_id
        self._content = str(text or "")
        self.update(self._renderable())

    @property
    def plain_text(self) -> str:
        return self._content

    def append(self, text: str) -> None:
        self._content += str(text or "")
        self.update(self._renderable())

    def finish(self, collapsed: bool = False) -> None:
        del collapsed
        self.update(self._renderable())

    def _title(self) -> Text:
        prefix = f"[{self.task_id}] " if self.task_id else ""
        if self.role == "user":
            return Text(f"{prefix}You", style="bold #60a5fa")
        if self.role == "thinking":
            return Text(f"{prefix}Thinking", style="bold #c084fc")
        return Text(f"{prefix}Assistant Output", style="bold #a8ff60")

    def _body(self) -> Text | Markdown:
        if self.role == "assistant":
            return Markdown(self._content)
        if self.role == "user":
            return Text(self._content, style="bold #ffffff")
        return Text(self._content, style="dim")

    def _border_style(self) -> str:
        if self.role == "user":
            return "#60a5fa"
        if self.role == "thinking":
            return "#6d28d9"
        return "#3f3f46"

    def _renderable(self) -> Panel:
        return Panel(
            self._body(),
            title=self._title(),
            border_style=self._border_style(),
            expand=True,
        )


class InfoBlock(Static):
    """Simple info widget that keeps plain text aligned with rendered output."""

    def __init__(self, text: str, *, style: str = "dim") -> None:
        super().__init__()
        self._renderable = _rich_markup_text(text, style=style)
        self.update(self._renderable)

    @property
    def plain_text(self) -> str:
        return self._renderable.plain


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
        background: #0d1117;
        border: tall #60d8ff;
    }
    ToolCard .tool-output {
        color: #60d8ff;
        padding: 0 1;
        overflow-x: hidden;
    }
    ToolCard .tool-output-scroll {
        max-height: 14;
        overflow-y: auto;
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
        self._content = ""
        self._collapsed = False
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
            VerticalScroll(self._output_widget, classes="tool-output-scroll"),
            title=self._label(),
            collapsed=self._collapsed,
        )
        self._sync_state()
        yield self._collapsible

    @property
    def is_expanded(self) -> bool:
        return not self._collapsed

    @property
    def output_text(self) -> str:
        return self._content

    @property
    def plain_text(self) -> str:
        parts = [self._label()]
        if self._content:
            parts.append(self._content)
        return "\n".join(parts)

    def _set_content(self, content: str) -> None:
        self._content = str(content or "")
        self._sync_state()

    def _sync_state(self) -> None:
        if self._output_widget:
            self._output_widget.update(Text(self._content))
        if self._collapsible:
            self._collapsible.title = self._label()
            self._collapsible.collapsed = self._collapsed

    def set_running(self) -> None:
        self.status = "running"
        self._collapsed = False
        self._sync_state()

    def set_success(self, content: str) -> None:
        self.status = "success"
        self._collapsed = True
        self._set_content(content)

    def set_error(self, content: str) -> None:
        self.status = "error"
        self._collapsed = False
        self._set_content(content)


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
        background: #0d1117;
        color: #60d8ff;
        padding: 0 1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._running_tool_cards: dict[str, ToolCard] = {}
        self._active_streams: dict[str, MessageBlock] = {}

    def renderable_text(self) -> str:
        """Return the visible chat text in a stable test-friendly form."""
        entries: list[str] = []
        for child in self.children:
            text = getattr(child, "plain_text", "")
            if text:
                entries.append(str(text))
        return "\n".join(entries)

    def _stream_key(self, role: str, task_id: str | None = None) -> str:
        return f"{task_id or 'root'}:{role}"

    def begin_stream(self, role: str, *, task_id: str | None = None) -> MessageBlock:
        key = self._stream_key(role, task_id=task_id)
        stream = self._active_streams.get(key)
        if stream is None:
            stream = MessageBlock(role, task_id=task_id)
            self.mount(stream)
            self._active_streams[key] = stream
        self.call_after_refresh(self.scroll_end, animate=False)
        return stream

    def finish_stream(
        self,
        role: str,
        *,
        task_id: str | None = None,
        collapsed: bool = False,
    ) -> None:
        key = self._stream_key(role, task_id=task_id)
        stream = self._active_streams.pop(key, None)
        if stream is None:
            return
        stream.finish(collapsed=collapsed)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_tool_call(self, name: str, args: dict | None = None, *, task_id: str | None = None) -> ToolCard:
        card = ToolCard(
            tool_name=name,
            args_summary=_format_args_summary(name, args),
            task_id=task_id,
        )
        self.mount(card)
        key = f"{task_id or ''}:{name}"
        self._running_tool_cards[key] = card
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
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_user_message(self, text: str) -> None:
        widget = MessageBlock("user", text)
        self.mount(widget)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_assistant_text(self, text: str) -> None:
        widget = MessageBlock("assistant", text)
        self.mount(widget)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_thinking(self, text: str) -> None:
        widget = MessageBlock("thinking", text)
        self.mount(widget)
        self.call_after_refresh(self.scroll_end, animate=False)

    def add_info(self, text: str, *, style: str = "dim") -> None:
        widget = InfoBlock(text, style=style)
        self.mount(widget)
        self.call_after_refresh(self.scroll_end, animate=False)

    def clear_log(self) -> None:
        self.remove_children()
        self._running_tool_cards.clear()
        self._active_streams.clear()


# ---------------------------------------------------------------------------
# StatusBar — bottom status bar showing model, tokens, cost, etc.
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Bottom status bar showing model info, token usage, cost, and phase."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #0d1117;
        color: #a8ff60;
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
        parts.append(f"[bold #a8ff60]{phase_icon} {self.phase}[/bold #a8ff60]")
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
            parts.append(f"  [bold #facc15]{self.cost_text}[/bold #facc15]")
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
        background: #0d1117;
        padding: 0 1;
        border-top: solid #a8ff60;
    }
    InputBar TextArea {
        width: 1fr;
        height: 1;
        background: #0d1117;
        color: #a8ff60;
        border: round #60d8ff;
    }
    InputBar Label {
        width: auto;
        content-align: left middle;
        color: #a8ff60;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_history = PromptHistory(_default_prompt_history_path())

    def compose(self) -> ComposeResult:
        yield Label("> ")
        yield CommandInput(
            history=self.prompt_history,
            placeholder="Type your message or /command",
            compact=True,
        )


class CommandInput(TextArea):
    """TextArea that owns chat submission while it has focus."""

    class MessageSubmitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    BINDINGS = [
        Binding("enter", "submit_message", "Send", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "New line", show=False, priority=True),
        Binding("up", "history_previous", "History previous", show=False, priority=True),
        Binding("down", "history_next", "History next", show=False, priority=True),
        Binding("tab", "complete_slash_command", "Complete command", show=False, priority=True),
    ]

    def __init__(
        self,
        *args: Any,
        history: PromptHistory | None = None,
        slash_commands: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_history = history
        self.slash_commands = slash_commands or ["/help"]

    def _is_single_line_or_empty(self) -> bool:
        return "\n" not in self.text

    def _set_text_value(self, value: str) -> None:
        self.load_text(value)
        line = len(self.text.splitlines()) - 1 if self.text else 0
        column = len(self.text.splitlines()[-1]) if self.text else 0
        self.move_cursor((line, column))

    def action_submit_message(self) -> None:
        """Delegate Enter to the app only from the focused command input."""
        value = self.text.strip()
        self.post_message(self.MessageSubmitted(value))
        if self.prompt_history and value:
            self.prompt_history.append(value)
        if hasattr(self.app, "action_submit_message"):
            self.app.action_submit_message()

    def action_insert_newline(self) -> None:
        """Keep Shift+Enter available for a multiline draft."""
        self.insert("\n")

    def action_history_previous(self) -> None:
        if not self.prompt_history or not self._is_single_line_or_empty():
            self.action_cursor_up()
            return
        self._set_text_value(self.prompt_history.previous())

    def action_history_next(self) -> None:
        if not self.prompt_history or not self._is_single_line_or_empty():
            self.action_cursor_down()
            return
        self._set_text_value(self.prompt_history.next())

    def action_complete_slash_command(self) -> None:
        if not self.text.startswith("/") or " " in self.text.strip():
            self.insert("\t")
            return
        matches = [command for command in self.slash_commands if command.startswith(self.text)]
        if len(matches) == 1:
            self._set_text_value(matches[0])
            return
        self.insert("\t")


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


def _rich_markup_text(text: str, *, style: str = "dim") -> Text:
    try:
        return Text.from_markup(_escape_invalid_markup_tags(text), style=style)
    except MarkupError:
        return Text(text, style=style)


def _escape_invalid_markup_tags(text: str) -> str:
    """Keep Rich markup while rendering bracketed command parameters literally."""

    def escape_if_invalid(match: re.Match[str]) -> str:
        tag = match.group(1)
        style = tag[1:] if tag.startswith("/") else tag
        try:
            Style.parse(style)
        except StyleSyntaxError:
            return f"\\[{tag}]"
        return match.group(0)

    return re.sub(r"(?<!\\)\[([^]]*)\]", escape_if_invalid, text)


def _default_prompt_history_path() -> Path:
    return Path.home() / ".paicli" / "history" / "prompt_history.txt"


# format_tokens, format_elapsed, format_cost are re-exported from _common above
