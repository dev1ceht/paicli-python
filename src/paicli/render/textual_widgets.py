"""Textual widgets for PaiCLI TUI.

Provides collapsible tool cards, chat log, status bar, and other widgets
that replace the Rich-based rendering with interactive Textual UI.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.errors import MarkupError, StyleSyntaxError
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Collapsible, Static, TextArea

from paicli.render._common import TOOL_LABELS as _TOOL_LABELS
from paicli.render._common import format_elapsed
from paicli.render.history import PromptHistory

_STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "idle": ("○", "o"),
    "running": ("●", "*"),
    "plan": ("◆", ">"),
    "thinking": ("◆", ">"),
    "success": ("✓", "OK"),
    "error": ("×", "ERR"),
}


def status_glyph(status: str, *, encoding: str | None = None) -> str:
    """Return an aligned Unicode status glyph or a readable ASCII fallback."""
    glyph, fallback = _STATUS_GLYPHS.get(status, ("○", "o"))
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        glyph.encode(target_encoding)
    except (LookupError, UnicodeEncodeError):
        return fallback
    return glyph


class MessageBlock(Static):
    """Mounted message block with test-visible content."""

    DEFAULT_CSS = """
    MessageBlock {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        background: #0d1117;
        border: none;
    }
    MessageBlock.message-user {
        background: #111827;
        border-left: solid #60a5fa;
    }
    MessageBlock.message-thinking {
        color: #c084fc;
        border-left: solid #6d28d9;
    }
    """

    def __init__(self, role: str, text: str = "", *, task_id: str | None = None) -> None:
        super().__init__()
        self.role = role
        self.task_id = task_id
        self._content = str(text or "")
        self.add_class(f"message-{role}")
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
        return Text(f"{prefix}Assistant Output", style="bold #8b949e")

    def _body(self) -> Text | Markdown:
        if self.role == "assistant":
            return Markdown(self._content)
        if self.role == "user":
            return Text(self._content, style="bold #ffffff")
        return Text(self._content, style="dim")

    def _renderable(self) -> Group:
        return Group(self._title(), self._body())


class InfoBlock(Static):
    """Simple info widget that keeps plain text aligned with rendered output."""

    def __init__(self, text: str, *, style: str = "dim") -> None:
        super().__init__()
        self._renderable = _rich_markup_text(text, style=style)
        self.update(self._renderable)

    @property
    def plain_text(self) -> str:
        return self._renderable.plain


class StartupBanner(Vertical):
    """Adaptive session introduction that remains visible during the session."""

    DEFAULT_CSS = """
    StartupBanner {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        background: #0d1117;
        border-left: solid #60d8ff;
    }
    StartupBanner .banner-compact {
        display: none;
        width: 100%;
        height: 3;
    }
    StartupBanner .banner-main {
        width: 100%;
        height: 4;
    }
    StartupBanner .banner-wordmark,
    StartupBanner .banner-pills,
    StartupBanner .banner-capabilities,
    StartupBanner .banner-workspace {
        width: 100%;
        height: 1;
    }
    StartupBanner .banner-wordmark {
        color: #f0f6fc;
        text-style: bold;
    }
    StartupBanner .banner-pill {
        width: auto;
        height: 1;
        margin-right: 2;
        padding: 0;
    }
    StartupBanner .banner-workspace {
        color: #8b949e;
    }
    StartupBanner.compact {
        height: 3;
    }
    StartupBanner.compact .banner-main {
        display: none;
    }
    StartupBanner.compact .banner-compact {
        display: block;
    }
    """

    def __init__(
        self,
        *,
        version: str,
        model: str,
        provider: str,
        hitl: str,
        tools: int,
        skills: int,
        mcp_servers: int,
        cwd: str,
    ) -> None:
        super().__init__()
        self._version = version
        self._model = model
        self._provider = provider
        self._hitl = hitl
        self._tools = tools
        self._skills = skills
        self._mcp_servers = mcp_servers
        self._cwd = cwd
        self._compact = False

    def _expanded_text(self) -> str:
        return (
            f"PAICLI  v{self._version} — Ready to build\n"
            f"{self._model} · {self._provider} · {self._hitl}\n"
            f"Tools: {self._tools} · Skills: {self._skills} · "
            f"MCP: {self._mcp_servers} servers\n"
            f"{self._cwd} · /help commands"
        )

    def _compact_text(self) -> str:
        return (
            f"PAICLI v{self._version} · Ready to build\n"
            f"{self._model} · {self._provider} · {self._hitl}\n"
            f"Tools: {self._tools} · Skills: {self._skills} · "
            f"MCP: {self._mcp_servers} · {self._cwd}"
        )

    @property
    def plain_text(self) -> str:
        if self._compact:
            return self._compact_text()
        return self._expanded_text()

    @property
    def is_compact(self) -> bool:
        return self._compact

    def on_resize(self, event: Any) -> None:
        """Use the approved three-row banner at the 80-column baseline."""
        compact = event.size.width <= 80
        if compact == self._compact:
            return
        self._compact = compact
        self.set_class(compact, "compact")

    def update_hitl(self, hitl: str) -> None:
        """Refresh the HITL summary without changing the banner's position."""
        self._hitl = hitl
        self.query_one("#banner-hitl", Static).update(Text(hitl, style="#60d8ff"))
        self.query_one(".banner-compact", Static).update(self._compact_text())

    def update_model(self, model: str, provider: str) -> None:
        """Refresh the active endpoint without replacing the banner."""
        self._model = model
        self._provider = provider
        self.query_one("#banner-model", Static).update(Text(model, style="#f0f6fc"))
        self.query_one("#banner-provider", Static).update(Text(provider, style="#60d8ff"))
        self.query_one(".banner-compact", Static).update(self._compact_text())

    def compose(self) -> ComposeResult:
        yield Static(self._compact_text(), classes="banner-compact")
        with Vertical(classes="banner-main"):
            yield Static(
                Text.assemble(
                    ("PAICLI", "bold #f0f6fc"),
                    (f"  v{self._version}", "#8b949e"),
                    (" — Ready to build", "#f0f6fc"),
                ),
                classes="banner-wordmark",
            )
            with Horizontal(classes="banner-pills"):
                yield Static(
                    Text(self._model, style="#f0f6fc"), id="banner-model", classes="banner-pill"
                )
                yield Static(
                    Text(self._provider, style="#60d8ff"),
                    id="banner-provider",
                    classes="banner-pill",
                )
                yield Static(
                    Text(self._hitl, style="#60d8ff"), id="banner-hitl", classes="banner-pill"
                )
            with Horizontal(classes="banner-capabilities"):
                yield Static(Text(f"Tools: {self._tools}"), classes="banner-pill")
                yield Static(Text(f"Skills: {self._skills}"), classes="banner-pill")
                yield Static(Text(f"MCP: {self._mcp_servers} servers"), classes="banner-pill")
            yield Static(
                Text.assemble(
                    (self._cwd, "#8b949e"),
                    (" · ", "#8b949e"),
                    ("/help", "#a8ff60"),
                    (" commands", "#8b949e"),
                ),
                classes="banner-workspace",
            )


# ---------------------------------------------------------------------------
# Activity rail — compact thinking and tool chronology
# ---------------------------------------------------------------------------


class ActivityRail(Vertical):
    """One visual group for consecutive Agent thinking and tool activity."""

    DEFAULT_CSS = """
    ActivityRail {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 0 0 1;
        background: #0d1117;
        border-left: solid #30363d;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._activities: list[Static] = []

    def compose(self) -> ComposeResult:
        yield from self._activities

    def add_activity(self, activity: Static) -> None:
        self._activities.append(activity)
        if self.is_mounted:
            self.mount(activity)

    @property
    def item_count(self) -> int:
        return len(self._activities)

    @property
    def plain_text(self) -> str:
        return "\n".join(
            str(text)
            for activity in self._activities
            if (text := getattr(activity, "plain_text", ""))
        )


class ThinkingBlock(Static):
    """Expandable thinking activity that recedes after completion."""

    DEFAULT_CSS = """
    ThinkingBlock {
        width: 100%;
        height: auto;
        color: #c084fc;
        background: #0d1117;
    }
    ThinkingBlock .thinking-output {
        color: #8b949e;
        padding: 0 1;
    }
    ThinkingBlock .thinking-output-scroll {
        max-height: 12;
        overflow-y: auto;
    }
    """

    def __init__(self, *, task_id: str | None = None) -> None:
        super().__init__()
        self.task_id = task_id
        self._content = ""
        self._collapsed = False
        self._complete = False
        self._started_at = time.monotonic()
        self._elapsed = 0.0
        self._pulse_frame = 0
        self._pulse_timer: Any = None
        self._animation_active = False
        self._collapsible: Collapsible | None = None
        self._output_widget: Static | None = None

    def on_mount(self) -> None:
        self._animation_active = True
        self._pulse_timer = self.set_interval(0.6, self._tick_pulse)

    def _tick_pulse(self) -> None:
        if not self._animation_active:
            return
        self._pulse_frame += 1
        self._sync_state()

    def compose(self) -> ComposeResult:
        self._output_widget = Static("", classes="thinking-output")
        self._collapsible = Collapsible(
            VerticalScroll(self._output_widget, classes="thinking-output-scroll"),
            title=self._label(),
            collapsed=self._collapsed,
        )
        self._sync_state()
        yield self._collapsible

    def _label(self) -> str:
        prefix = f"[{self.task_id}] " if self.task_id else ""
        if self._complete:
            return (
                f"{prefix}{status_glyph('thinking')} Thinking complete · "
                f"{format_elapsed(self._elapsed)}"
            )
        pulse = "thinking" if self._pulse_frame % 2 == 0 else "idle"
        return f"{prefix}{status_glyph(pulse)} Thinking"

    @property
    def plain_text(self) -> str:
        return f"{self._label()}\n{self._content}" if self._content else self._label()

    @property
    def is_expanded(self) -> bool:
        return not self._collapsed

    @property
    def animation_active(self) -> bool:
        return self._animation_active

    def append(self, text: str) -> None:
        self._content += str(text or "")
        self._sync_state()

    def finish(self, collapsed: bool = True) -> None:
        self._complete = True
        self._animation_active = False
        if self._pulse_timer is not None:
            self._pulse_timer.pause()
        self._elapsed = max(0.0, time.monotonic() - self._started_at)
        self._collapsed = collapsed
        self._sync_state()

    def _sync_state(self) -> None:
        if self._output_widget:
            self._output_widget.update(Text(self._content))
        if self._collapsible:
            self._collapsible.title = self._label()
            self._collapsible.collapsed = self._collapsed


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
        margin: 0;
        padding: 0;
        background: #0d1117;
        border: none;
    }
    ToolCard .tool-output {
        color: #c9d1d9;
        padding: 0 1;
        overflow-x: hidden;
    }
    ToolCard .tool-output-scroll {
        max-height: 14;
        overflow-y: auto;
    }
    ToolCard.tool-running {
        color: #60d8ff;
    }
    ToolCard.tool-success {
        color: #a8ff60;
    }
    ToolCard.tool-error {
        color: #ff4d5a;
        border-left: solid #ff4d5a;
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
        self._error_summary = ""
        self._retries: list[str] = []
        self._collapsed = False
        self._started_at = time.monotonic()
        self._elapsed = 0.0
        self._pulse_frame = 0
        self._pulse_timer: Any = None
        self._animation_active = False
        self._collapsible: Collapsible | None = None
        self._output_widget: Static | None = None

    def on_mount(self) -> None:
        self._animation_active = self.status == "running"
        self._pulse_timer = self.set_interval(0.6, self._tick_pulse)

    def _tick_pulse(self) -> None:
        if not self._animation_active:
            return
        self._pulse_frame += 1
        self._sync_state()

    def _label(self) -> str:
        glyph_status = self.status
        if self.status == "running" and self._pulse_frame % 2:
            glyph_status = "idle"
        icon = status_glyph(glyph_status)
        status_text = {
            "running": "Running",
            "success": "Success",
            "error": "Failed",
        }.get(self.status, self.status.title())
        prefix = f"[{self.task_id}] " if self.task_id else ""
        elapsed = f" · {format_elapsed(self._elapsed)}" if self.status != "running" else ""
        error = f" — {self._error_summary}" if self._error_summary else ""
        retry = f" · {self._retries[-1]}" if self._retries else ""
        if self.args_summary:
            return (
                f"{prefix}{icon} {status_text} · {self.tool_name}: "
                f"{self.args_summary}{retry}{elapsed}{error}"
            )
        return f"{prefix}{icon} {status_text} · {self.tool_name}{retry}{elapsed}{error}"

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
    def error_summary(self) -> str:
        return self._error_summary

    @property
    def retry_count(self) -> int:
        return len(self._retries)

    @property
    def animation_active(self) -> bool:
        return self._animation_active

    def record_retry(
        self,
        *,
        attempt: int,
        max_retries: int,
        error_kind: str,
        delay: float,
    ) -> None:
        self._retries.append(
            f"retry {attempt}/{max_retries} · {error_kind} · {format_elapsed(delay)}"
        )
        self._sync_state()

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
        self.remove_class("tool-running", "tool-success", "tool-error")
        self.add_class(f"tool-{self.status}")
        if self._output_widget:
            self._output_widget.update(Text(self._content))
        if self._collapsible:
            self._collapsible.title = self._label()
            self._collapsible.collapsed = self._collapsed

    def set_running(self) -> None:
        self.status = "running"
        self._animation_active = True
        if self._pulse_timer is not None:
            self._pulse_timer.resume()
        self._error_summary = ""
        self._collapsed = False
        self._sync_state()

    def set_success(self, content: str) -> None:
        self.status = "success"
        self._animation_active = False
        if self._pulse_timer is not None:
            self._pulse_timer.pause()
        self._elapsed = max(0.0, time.monotonic() - self._started_at)
        self._collapsed = True
        self._set_content(content)

    def set_error(self, content: str) -> None:
        self.status = "error"
        self._animation_active = False
        if self._pulse_timer is not None:
            self._pulse_timer.pause()
        self._elapsed = max(0.0, time.monotonic() - self._started_at)
        self._error_summary = next(
            (line.strip()[:160] for line in str(content or "").splitlines() if line.strip()),
            "Tool failed",
        )
        self._collapsed = False
        self._set_content(content)


class NewActivityIndicator(Static):
    """Keyboard and mouse affordance for resuming conversation follow mode."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "Latest", show=False),
        Binding("end", "activate", "Latest", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("↓ New activity · Ctrl+End", classes="new-activity-indicator")

    def action_activate(self) -> None:
        resume = getattr(self.parent, "resume_following", None)
        if callable(resume):
            resume()

    def on_click(self) -> None:
        self.action_activate()


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
    ChatLog .new-activity-indicator {
        display: none;
        dock: bottom;
        width: auto;
        height: 1;
        padding: 0 1;
        background: #16130b;
        color: #facc15;
        text-style: bold;
    }
    ChatLog.new-activity .new-activity-indicator {
        display: block;
    }
    ChatLog .new-activity-indicator:focus {
        background: #1b2430;
        color: #60d8ff;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._running_tool_cards: dict[str, ToolCard] = {}
        self._active_streams: dict[str, MessageBlock | ThinkingBlock] = {}
        self._current_activity_rail: ActivityRail | None = None
        self._follow_mode = True
        self._new_activity_pending = False

    def compose(self) -> ComposeResult:
        yield NewActivityIndicator()

    @property
    def new_activity_pending(self) -> bool:
        return self._new_activity_pending

    def _set_new_activity_pending(self, pending: bool) -> None:
        self._new_activity_pending = pending
        self.set_class(pending, "new-activity")

    def _scroll_to_latest(self) -> None:
        self.scroll_end(animate=False, immediate=True)
        self._set_new_activity_pending(False)

    def _follow_new_activity(self) -> None:
        if self._follow_mode and self.is_vertical_scroll_end:
            self.call_after_refresh(self._scroll_to_latest)
            return
        self._follow_mode = False
        self._set_new_activity_pending(True)

    def pause_following(self) -> None:
        self._follow_mode = False

    def resume_following(self) -> None:
        self._follow_mode = True
        self._set_new_activity_pending(False)
        self.call_after_refresh(self._scroll_to_latest)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Resume following when the user manually returns to the bottom."""
        super().watch_scroll_y(old_value, new_value)
        if getattr(self, "_new_activity_pending", False) and new_value >= self.max_scroll_y:
            self._follow_mode = True
            self._set_new_activity_pending(False)

    def _activity_rail(self) -> ActivityRail:
        if self._current_activity_rail is None:
            self._current_activity_rail = ActivityRail()
            self.mount(self._current_activity_rail)
        return self._current_activity_rail

    def add_startup_banner(self, banner: StartupBanner) -> None:
        """Keep the session introduction first in conversation order."""
        self.mount(banner, before=self.query_one(NewActivityIndicator))

    def _close_activity_rail(self) -> None:
        self._current_activity_rail = None

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

    def begin_stream(
        self,
        role: str,
        *,
        task_id: str | None = None,
    ) -> MessageBlock | ThinkingBlock:
        key = self._stream_key(role, task_id=task_id)
        stream = self._active_streams.get(key)
        if stream is None:
            if role == "thinking":
                stream = ThinkingBlock(task_id=task_id)
                self._activity_rail().add_activity(stream)
            else:
                self._close_activity_rail()
                stream = MessageBlock(role, task_id=task_id)
                self.mount(stream)
            self._active_streams[key] = stream
        self._follow_new_activity()
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
        stream.finish(collapsed=collapsed or role == "thinking")
        self._follow_new_activity()

    def add_tool_call(
        self, name: str, args: dict | None = None, *, task_id: str | None = None
    ) -> ToolCard:
        card = ToolCard(
            tool_name=name,
            args_summary=_format_args_summary(name, args),
            task_id=task_id,
        )
        self._activity_rail().add_activity(card)
        key = f"{task_id or ''}:{name}"
        self._running_tool_cards[key] = card
        self._follow_new_activity()
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
        self._follow_new_activity()

    def add_user_message(self, text: str) -> None:
        self._close_activity_rail()
        widget = MessageBlock("user", text)
        self.mount(widget)
        self._follow_new_activity()

    def add_assistant_text(self, text: str) -> None:
        self._close_activity_rail()
        widget = MessageBlock("assistant", text)
        self.mount(widget)
        self._follow_new_activity()

    def add_thinking(self, text: str) -> None:
        widget = ThinkingBlock()
        widget.append(text)
        widget.finish()
        self._activity_rail().add_activity(widget)
        self._follow_new_activity()

    def record_tool_retry(
        self,
        name: str,
        *,
        attempt: int,
        max_retries: int,
        error_kind: str,
        delay: float,
    ) -> bool:
        for card in reversed(list(self._running_tool_cards.values())):
            if card.tool_name == name:
                card.record_retry(
                    attempt=attempt,
                    max_retries=max_retries,
                    error_kind=error_kind,
                    delay=delay,
                )
                return True
        return False

    def add_info(self, text: str, *, style: str = "dim") -> None:
        self._close_activity_rail()
        widget = InfoBlock(text, style=style)
        self.mount(widget)
        self._follow_new_activity()

    def add_inline_decision(self, widget: Static) -> None:
        """Mount a blocking decision without navigating away from the canvas."""
        self._close_activity_rail()
        self.mount(widget)
        self._follow_new_activity()

    def clear_log(self) -> None:
        for child in list(self.children):
            if isinstance(child, NewActivityIndicator):
                continue
            child.remove()
        self._running_tool_cards.clear()
        self._active_streams.clear()
        self._current_activity_rail = None
        self._follow_mode = True
        self._set_new_activity_pending(False)

    def clear_conversation(self) -> None:
        """Remove conversation widgets while preserving the startup banner."""
        for child in list(self.children):
            if isinstance(child, StartupBanner | NewActivityIndicator):
                continue
            child.remove()
        self._running_tool_cards.clear()
        self._active_streams.clear()
        self._current_activity_rail = None
        self._follow_mode = True
        self._set_new_activity_pending(False)


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
    context_level: reactive[str] = reactive("neutral")
    pressure_text: reactive[str] = reactive("pressure:—")
    token_detail: reactive[str] = reactive("")
    cost_text: reactive[str] = reactive("")
    elapsed_text: reactive[str] = reactive("")

    def render(self) -> str:
        parts: list[str] = []
        # Phase
        phase_icon = status_glyph(self.phase)
        phase_color = {
            "idle": "#8b949e",
            "running": "#60d8ff",
            "plan": "#c084fc",
        }.get(self.phase, "#8b949e")
        parts.append(f"[bold {phase_color}]{phase_icon} {self.phase}[/bold {phase_color}]")
        # Model
        if self.model:
            parts.append(f"  [bold]{self.model}[/bold]")
        # Context
        context_color = {
            "normal": "#a8ff60",
            "yellow": "#facc15",
            "orange": "#fb923c",
            "red": "#ef4444",
            "neutral": "#94a3b8",
        }.get(self.context_level, "#94a3b8")
        parts.append(f"  [{context_color}]{self.context_text}[/{context_color}]")
        # Compression pressure after context assembly
        parts.append(f"  [dim]{self.pressure_text}[/dim]")
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
        height: 2;
        background: #0d1117;
        padding: 0 1;
    }
    InputBar TextArea {
        width: 1fr;
        height: 2;
        background: #0d1117;
        color: #f0f6fc;
        border: none;
        border-top: solid #30363d;
    }
    InputBar TextArea:focus {
        border-top: solid #60d8ff;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_history = PromptHistory(_default_prompt_history_path())

    def compose(self) -> ComposeResult:
        yield CommandInput(
            history=self.prompt_history,
            placeholder="输入消息，Enter 发送，Shift+Enter 换行",
            compact=True,
        )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Resize the dock after typing and paste operations."""
        if isinstance(event.text_area, CommandInput):
            event.text_area.sync_height()


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
        self.sync_height()

    def on_mount(self) -> None:
        self.sync_height()

    def sync_height(self) -> None:
        """Keep the command dock compact while allowing multiline drafts."""
        target = min(5, max(2, self.text.count("\n") + 1))
        self.styles.height = target
        if isinstance(self.parent, InputBar):
            self.parent.styles.height = target

    def action_submit_message(self) -> None:
        """Delegate Enter to the app only from the focused command input."""
        value = self.text.strip()
        self.post_message(self.MessageSubmitted(value))
        if self.prompt_history and value:
            self.prompt_history.append(value)
        if hasattr(self.app, "action_submit_message"):
            self.app.action_submit_message()
        self.sync_height()

    def action_insert_newline(self) -> None:
        """Keep Shift+Enter available for a multiline draft."""
        self.insert("\n")
        self.sync_height()

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


# Formatting helpers live in paicli.render._common.
