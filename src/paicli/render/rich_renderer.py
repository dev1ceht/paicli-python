from __future__ import annotations

import json
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class RichRenderer:
    def __init__(
        self,
        console: Console | None = None,
        *,
        live_markdown: bool = False,
        context_window: int | None = None,
    ):
        self.console = console or Console()
        self._buffer: list[str] = []
        self._thinking_buffer: list[str] = []
        self._live_markdown = live_markdown
        self._live: Live | None = None
        self._thinking_live: Live | None = None
        self._context_window = context_window or 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._last_input_tokens = 0
        self._last_turns = 0
        self._last_total_tokens = 0
        self._last_context_ratio = 0.0
        self._last_has_usage = False

    def set_context_window(self, context_window: int | None) -> None:
        self._context_window = context_window or self._context_window

    def start_run(self) -> None:
        self._buffer.clear()
        self._thinking_buffer.clear()
        self._stop_live_markdown()
        self._stop_live_thinking()
        self._input_tokens = 0
        self._output_tokens = 0
        self._last_input_tokens = 0

    def toolbar_status(self) -> dict[str, Any]:
        return {
            "turns": self._last_turns,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": self._last_total_tokens,
            "context_ratio": self._last_context_ratio,
            "has_usage": self._last_has_usage,
        }

    def banner(
        self,
        *,
        model: str,
        provider: str,
        cwd: str,
        tools: int,
        version: str = "0.1.0",
        api_key_configured: bool = False,
        mcp_servers: int = 0,
        skills: int = 0,
        agents_files: int = 0,
        hitl_mode: str = "auto",
    ) -> None:
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(ratio=2)
        top.add_row(
            self._identity_panel(version=version, api_key_configured=api_key_configured),
            self._release_panel(version=version),
        )

        _ = model, provider, cwd, tools, mcp_servers, skills, agents_files, hitl_mode

        self.console.print()
        self.console.print(top)
        self.console.print(Align.right(Text("? for shortcuts", style="dim")))
        self.console.rule(style="grey23")
        self.console.print()

    def handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "text_delta":
            self._flush_thinking()
            text = str(event.get("text") or "")
            self._buffer.append(text)
            self._update_live_markdown()
        elif event_type == "thinking_delta":
            thinking = str(event.get("thinking") or "")
            self._thinking_buffer.append(thinking)
            self._update_live_thinking()
        elif event_type == "usage":
            self._record_usage(event.get("usage") or {})
        elif event_type == "turn_complete":
            stop_reason = str(event.get("stop_reason") or "end_turn")
            title = "Assistant Output" if stop_reason == "tool_use" else "Final Output"
            self._flush_thinking()
            self._flush_markdown(title=title)
        elif event_type == "tool_call":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_tool_call(event)
        elif event_type == "tool_result":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_tool_result(event)
        elif event_type == "error":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(f"[red]Error:[/red] {event.get('error')}")
        elif event_type == "plan_generation_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print("[bold cyan]?[/bold cyan] 使用 Plan-and-Execute 模式")
            self.console.print(f"[bold cyan]?[/bold cyan] 正在规划任务: {event.get('goal')}")
        elif event_type == "plan_thinking":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            thinking = str(event.get("thinking") or "")
            if thinking.strip():
                self.console.print(
                    _output_panel(
                        Text(thinking, style="dim"),
                        title=Text("规划思考", style="bold cyan"),
                        border_style="cyan",
                    )
                )
        elif event_type == "plan_review_summary":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(str(event.get("summary") or ""))
        elif event_type == "plan_visualization":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(str(event.get("visualization") or ""))
        elif event_type == "plan_review_instructions":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(
                "计划已生成。\n"
                "- Enter: 按当前计划执行\n"
                "- Ctrl+O: 展开完整计划\n"
                "- ESC: 折叠或取消本次计划\n"
                "- I: 输入补充要求后重新规划"
            )
        elif event_type == "plan_cancelled":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print("[yellow]计划已取消。[/yellow]")
        elif event_type == "plan_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_plan(event)
        elif event_type == "task_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            task = event.get("task") or {}
            self.console.print(f"[bold #60a5fa]Plan task:[/bold #60a5fa] {task.get('id')}")
        elif event_type == "task_completed":
            self.console.print(f"[green]Task completed:[/green] {event.get('task_id')}")
        elif event_type == "task_failed":
            self.console.print(
                f"[red]Task failed:[/red] {event.get('task_id')} {event.get('error')}"
            )
        elif event_type == "task_skipped":
            self.console.print(f"[yellow]Task skipped:[/yellow] {event.get('task_id')}")
        elif event_type == "plan_failed":
            detail = event.get("error") or event.get("failed")
            self.console.print(f"[red]Plan failed:[/red] {detail}")
        elif event_type == "plan_completed":
            self.console.print("[green]Plan completed[/green]")
        elif event_type == "done":
            self._flush_thinking()
            self._flush_markdown(title="Final Output")
            self._record_run_summary(event)

    def markdown(self, text: str) -> None:
        self.console.print(Markdown(text))

    def newline(self) -> None:
        self._flush_thinking()
        self._flush_markdown(title="Final Output")
        self.console.print()

    def _flush_markdown(self, *, title: str) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._stop_live_markdown()
        if text.strip():
            self.console.print(
                _output_panel(
                    Markdown(text),
                    title=Text(title, style="bold #a8ff60"),
                    border_style="#3f3f46",
                )
            )

    def _update_live_markdown(self) -> None:
        if not self._live_markdown or not self.console.is_terminal:
            return
        text = "".join(self._buffer)
        if not text.strip():
            return
        renderable = _output_panel(
            Markdown(text),
            title=Text("Assistant Output", style="bold #a8ff60"),
            border_style="#3f3f46",
        )
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="visible",
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=True)

    def _stop_live_markdown(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def _flush_thinking(self) -> None:
        if not self._thinking_buffer:
            return
        text = "".join(self._thinking_buffer)
        self._thinking_buffer.clear()
        self._stop_live_thinking()
        if text.strip():
            self.console.print(
                _output_panel(
                    Text(text, style="dim"),
                    title=Text("Thinking", style="bold #c084fc"),
                    border_style="#6d28d9",
                )
            )

    def _update_live_thinking(self) -> None:
        if not self._live_markdown or not self.console.is_terminal:
            return
        text = "".join(self._thinking_buffer)
        if not text.strip():
            return
        renderable = _output_panel(
            Text(text, style="dim"),
            title=Text("Thinking", style="bold #c084fc"),
            border_style="#6d28d9",
        )
        if self._thinking_live is None:
            self._thinking_live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="visible",
            )
            self._thinking_live.start(refresh=True)
            return
        self._thinking_live.update(renderable, refresh=True)

    def _stop_live_thinking(self) -> None:
        if self._thinking_live is None:
            return
        self._thinking_live.stop()
        self._thinking_live = None

    def _record_usage(self, usage: dict[str, Any]) -> None:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        if input_tokens:
            self._last_input_tokens = input_tokens

    def _print_tool_call(self, event: dict[str, Any]) -> None:
        name = str(event.get("name") or "unknown")
        payload = event.get("input") or {}
        body = Table.grid(padding=(0, 1))
        body.add_column(style="dim", no_wrap=True)
        body.add_column()
        body.add_row("name", Text(name, style="bold #facc15"))
        body.add_row("input", Text(_format_payload(payload), style="#e5e7eb"))
        self.console.print(
            _output_panel(
                body,
                title=Text("Tool Use", style="bold #facc15"),
                border_style="#facc15",
            )
        )

    def _print_tool_result(self, event: dict[str, Any]) -> None:
        is_error = bool(event.get("is_error"))
        name = str(event.get("name") or "unknown")
        result = str(event.get("result") or "")
        if len(result) > 1200:
            result = result[:1200] + "\n... [truncated]"
        title_style = "bold #ff4d5a" if is_error else "bold #22c55e"
        border_style = "#ff4d5a" if is_error else "#22c55e"
        status = "error" if is_error else "ok"
        self.console.print(
            _output_panel(
                result or "(empty result)",
                title=Text(f"Tool Result · {name} · {status}", style=title_style),
                border_style=border_style,
            )
        )

    def _print_plan(self, event: dict[str, Any]) -> None:
        table = Table(title="Execution Plan")
        table.add_column("ID", style="bold #60a5fa", no_wrap=True)
        table.add_column("Depends On", style="dim")
        table.add_column("Task")
        for task in event.get("tasks") or []:
            depends_on = ", ".join(task.get("depends_on") or [])
            table.add_row(
                str(task.get("id") or ""),
                depends_on or "-",
                str(task.get("description") or ""),
            )
        self.console.print(table)

    def _record_run_summary(self, event: dict[str, Any]) -> None:
        total_tokens = int(event.get("total_tokens") or self._input_tokens + self._output_tokens)
        turns = int(event.get("total_turns") or 0)
        has_usage = total_tokens > 0 or self._input_tokens > 0 or self._output_tokens > 0
        context_ratio = (
            self._last_input_tokens / self._context_window if self._context_window > 0 else 0
        )
        self._last_turns = turns
        self._last_total_tokens = total_tokens
        self._last_context_ratio = context_ratio
        self._last_has_usage = has_usage

    def _identity_panel(self, *, version: str, api_key_configured: bool) -> Table:
        logo = Text("\n".join(_PI_LOGO), style="bold #a8ff60")
        identity = Text()
        identity.append("PaiCLI ", style="bold white")
        identity.append(f"v{version}", style="dim")
        identity.append("\n\n")
        if api_key_configured:
            identity.append("Signed in ", style="bold white")
            identity.append("API Key", style="dim")
        else:
            identity.append("Missing ", style="bold red")
            identity.append("API Key", style="dim")

        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column()
        grid.add_row(logo, Align.center(identity, vertical="middle"))
        return grid

    def _release_panel(self, *, version: str) -> Panel:
        notes = Text()
        for line in [
            "π logo home layout for the interactive CLI",
            "MCP, skills, tools, and workspace status at a glance",
            "Use /help for commands and /config for runtime settings",
        ]:
            notes.append("- ", style="dim")
            notes.append(line, style="dim")
            notes.append("\n")
        notes.append("/help", style="purple")
        notes.append(" for more", style="dim")
        return Panel(
            notes,
            title=Text(f"What's new (v{version})", style="bold green"),
            border_style="grey37",
            box=box.ROUNDED,
            padding=(0, 2),
        )


_PI_LOGO = (
    "████████████",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
)


def _format_payload(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except TypeError:
        return str(payload)


def _output_panel(renderable: Any, *, title: Text, border_style: str) -> Panel:
    return Panel(
        renderable,
        title=title,
        border_style=border_style,
        box=box.ROUNDED,
        expand=True,
    )
