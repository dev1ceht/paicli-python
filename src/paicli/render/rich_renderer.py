from __future__ import annotations

import time
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from paicli.context.telemetry import ContextUsageState

# ---------------------------------------------------------------------------
# Import shared utilities from _common (single source of truth)
# ---------------------------------------------------------------------------
from paicli.render._common import (
    NO_COLOR as _NO_COLOR,
)
from paicli.render._common import (
    PI_LOGO as _PI_LOGO,
)
from paicli.render._common import (
    TOOL_LABELS as _TOOL_LABELS,
)
from paicli.render._common import (
    diff_ops as _diff_ops,
)
from paicli.render._common import (
    estimate_cost,
    format_cost,
    format_elapsed,
    format_tokens,
)
from paicli.render._common import (
    format_payload as _format_payload,
)
from paicli.render._common import (
    shorten_home as _shorten_home,
)
from paicli.render._common import (
    tool_label as _tool_label,
)

# ---------------------------------------------------------------------------
# RichRenderer
# ---------------------------------------------------------------------------


class RichRenderer:
    def __init__(
        self,
        console: Console | None = None,
        *,
        live_markdown: bool = False,
        context_window: int | None = None,
    ):
        self.console = console or Console(no_color=_NO_COLOR)
        self._buffer: list[str] = []
        self._thinking_buffer: list[str] = []
        self._live_markdown = live_markdown
        self._live: Live | None = None
        self._thinking_live: Live | None = None
        self._context_window = context_window or 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._last_input_tokens = 0
        self._last_output_tokens = 0
        self._last_cached_tokens = 0
        self._last_turns = 0
        self._last_total_tokens = 0
        self._last_context_ratio = 0.0
        self._last_has_usage = False
        self._pressure_tier: str | None = None
        # Elapsed time tracking
        self._run_start_time: float | None = None
        self._last_elapsed: float = 0.0
        # Cost tracking
        self._provider = ""
        self._session_cost: float = 0.0
        self._last_cost: float = 0.0
        # Phase tracking
        self._phase = "idle"  # idle, running, plan
        # Per-task streaming buffers
        self._task_buffers: dict[str, list[str]] = {}
        self._task_thinking_buffers: dict[str, list[str]] = {}
        self._context_usage = ContextUsageState()

    def set_context_window(self, context_window: int | None) -> None:
        self._context_window = context_window or self._context_window

    def set_provider(self, provider: str) -> None:
        self._provider = provider

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def start_run(self) -> None:
        self._buffer.clear()
        self._thinking_buffer.clear()
        self._stop_live_markdown()
        self._stop_live_thinking()
        # Start a new accounting scope while preserving the last displayed
        # request metrics until the next usage event arrives.
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._task_buffers.clear()
        self._task_thinking_buffers.clear()
        self._run_start_time = time.monotonic()
        self._phase = "running"

    def toolbar_status(self) -> dict[str, Any]:
        elapsed = self._last_elapsed
        if self._run_start_time and self._phase in {"running", "plan"}:
            elapsed = time.monotonic() - self._run_start_time
        status = {
            "turns": self._last_turns,
            "input_tokens": self._last_input_tokens,
            "output_tokens": self._last_output_tokens,
            "cached_tokens": self._last_cached_tokens,
            "total_tokens": self._last_total_tokens,
            "context_ratio": self._last_context_ratio,
            "context_window": self._context_window,
            "has_usage": self._last_has_usage,
            "pressure_tier": self._pressure_tier,
            "elapsed": elapsed,
            "cost": self._last_cost,
            "phase": self._phase,
            "provider": self._provider,
        }
        reading = self._context_usage.current
        if reading is not None:
            reading_state = str(reading.get("state") or "")
            context_window = reading.get("context_window")
            used_tokens = int(reading.get("used_tokens") or 0)
            status.update(
                {
                    "context_used_tokens": used_tokens,
                    "context_window": context_window,
                    "context_ratio": (
                        used_tokens / int(context_window) if context_window else 0.0
                    ),
                    "context_estimated": bool(reading.get("estimated")),
                    "context_active_count": self._context_usage.active_count,
                    "pressure_ratio": reading.get("pressure_ratio"),
                    "pressure_estimated": bool(reading.get("estimated")),
                }
            )
            if reading_state != "retained":
                status.update(
                    {
                        "input_tokens": int(reading.get("input_tokens") or 0),
                        "output_tokens": int(reading.get("output_tokens") or 0),
                        "cached_tokens": int(reading.get("cached_tokens") or 0),
                        "has_usage": True,
                        "usage_estimated": bool(reading.get("estimated")),
                    }
                )
            elif status["has_usage"]:
                status["usage_label"] = "last"
        return status

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
        self._provider = provider
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(ratio=2)
        top.add_row(
            self._identity_panel(version=version, api_key_configured=api_key_configured),
            self._workspace_panel(
                model=model,
                provider=provider,
                cwd=cwd,
                tools=tools,
                mcp_servers=mcp_servers,
                skills=skills,
                agents_files=agents_files,
                hitl_mode=hitl_mode,
            ),
        )

        self.console.print()
        self.console.print(top)
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
        elif event_type in {
            "context_usage",
            "context_request_finished",
            "context_pending_clear",
            "context_scope_clear",
        }:
            self._context_usage.apply(event)
        elif event_type == "retry":
            scope = event.get("scope", "call")
            target = event.get("tool_name") or event.get("model") or ""
            self.console.print(
                f"[yellow]Retrying {scope} {target} "
                f"({event.get('attempt')}/{event.get('max_retries')}) "
                f"after {float(event.get('delay') or 0):.2f}s "
                f"[{event.get('error_kind', 'unknown')}][/yellow]"
            )
        elif event_type == "retry_exhausted":
            target = event.get("tool_name") or event.get("model") or ""
            self.console.print(
                f"[bold red]Retry exhausted for {event.get('scope', 'call')} {target} "
                f"after {event.get('attempt')} retries "
                f"[{event.get('error_kind', 'unknown')}][/bold red]"
            )
        elif event_type == "context_status":
            self._pressure_tier = event.get("pressure_tier")
        elif event_type == "context_reduced":
            before = round(float(event.get("before_ratio") or 0) * 100)
            after = round(float(event.get("after_ratio") or 0) * 100)
            actions = ", ".join(
                str(action).replace("_", " ") for action in event.get("actions") or []
            )
            self.console.print(f"[dim]Context reduced: {before}% → {after}% · {actions}[/dim]")
        elif event_type == "turn_complete":
            stop_reason = str(event.get("stop_reason") or "end_turn")
            title = "Assistant Output" if stop_reason == "tool_use" else "Final Output"
            self._flush_thinking()
            # Java: answer marker "▪" before final output
            if stop_reason != "tool_use" and self._buffer:
                self.console.print(Text("\u25aa", style="bold #22c55e"))
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
            self.console.print(f"[bold red]\u274c Error:[/bold red] {event.get('error')}")
        # -- Plan events -----------------------------------------------
        elif event_type == "plan_generation_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._phase = "plan"
            self.console.print(
                Text("\U0001f4cb \u4f7f\u7528 Plan-and-Execute \u6a21\u5f0f", style="bold cyan")
            )
            self.console.print(f"  \u6b63\u5728\u89c4\u5212\u4efb\u52a1: {event.get('goal')}")
        elif event_type == "plan_thinking":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            thinking = str(event.get("thinking") or "")
            if thinking.strip():
                self.console.print(
                    _output_panel(
                        Text(thinking, style="dim"),
                        title=Text("\U0001f9e0 \u89c4\u5212\u601d\u8003", style="bold cyan"),
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
                "[dim]\u8ba1\u5212\u5df2\u751f\u6210\u3002[/dim]\n"
                "  [bold]Enter[/bold]  \u6309\u5f53\u524d\u8ba1\u5212\u6267\u884c\n"
                "  [bold]Ctrl+O[/bold] \u5c55\u5f00\u5b8c\u6574\u8ba1\u5212\n"
                "  [bold]ESC[/bold]    \u6298\u53e0\u6216\u53d6\u6d88\u672c\u6b21\u8ba1\u5212\n"
                "  [bold]I[/bold]      \u8f93\u5165\u8865\u5145\u8981\u6c42\u540e\u91cd\u65b0\u89c4\u5212"
            )
        elif event_type == "plan_cancelled":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._phase = "idle"
            self._record_run_summary({})
            self.console.print(
                "[yellow]\u23f9\ufe0f \u5df2\u53d6\u6d88\u672c\u6b21\u8ba1\u5212\u6267\u884c\u3002[/yellow]"
            )
        elif event_type == "plan_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._phase = "plan"
            self.console.print(
                Text("\U0001f680 \u5f00\u59cb\u6267\u884c\u8ba1\u5212...", style="bold green")
            )
            self._print_plan(event)
        # -- Task events -----------------------------------------------
        elif event_type == "task_started":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            task = event.get("task") or {}
            task_id = task.get("id", "?")
            task_type = task.get("type", "COMMAND")
            self._task_buffers[task_id] = []
            self._task_thinking_buffers[task_id] = []
            self.console.print(
                f"\u25b6\ufe0f [bold #60a5fa]\u6267\u884c\u4efb\u52a1:[/bold #60a5fa] "
                f"{task_id} [dim][{task_type}][/dim]"
            )
        elif event_type == "task_completed":
            task_id = event.get("task_id")
            duration = event.get("duration")
            duration_str = f" ({format_elapsed(duration)})" if duration else ""
            self._flush_task_output(task_id)
            self.console.print(f"\u2705 [green]\u5b8c\u6210[/green] {task_id}{duration_str}")
        elif event_type == "task_failed":
            task_id = event.get("task_id")
            self._flush_task_output(task_id)
            self.console.print(
                f"\u274c [red]\u4efb\u52a1\u5931\u8d25:[/red] {task_id} {event.get('error')}"
            )
        elif event_type == "task_skipped":
            self.console.print(
                f"\u23ed\ufe0f [yellow]\u4efb\u52a1\u8df3\u8fc7:[/yellow] {event.get('task_id')}"
            )
        elif event_type == "task_blocked":
            self.console.print(
                f"⛔ [yellow]任务阻塞:[/yellow] {event.get('task_id')} "
                f"依赖={event.get('dependencies') or []}"
            )
        elif event_type == "plan_failed":
            self._phase = "idle"
            self._record_run_summary({})
            detail = event.get("error") or event.get("failed")
            self.console.print(f"[bold red]\u274c \u8ba1\u5212\u5931\u8d25:[/bold red] {detail}")
            results = event.get("results") or {}
            if results:
                self.console.print(f"[dim]部分完成: {len(results)} 个任务[/dim]")
        elif event_type == "plan_aggregate_result":
            completed = event.get("completed") or {}
            failed = event.get("failed") or {}
            blocked = event.get("blocked") or {}
            self.console.print(
                f"[bold cyan]Execution aggregate:[/bold cyan] "
                f"status={event.get('status')} attempts={event.get('attempts')} "
                f"completed={len(completed)} failed={len(failed)} "
                f"blocked={len(blocked)}"
            )
        elif event_type == "plan_completed":
            self._phase = "idle"
            self._record_run_summary({})
            results = event.get("results") or {}
            self.console.print(
                Text("\n\u2705 \u8ba1\u5212\u6267\u884c\u5b8c\u6210\uff01", style="bold green")
            )
            if results:
                self.console.print(
                    Text(f"  \u5171\u5b8c\u6210 {len(results)} \u4e2a\u4efb\u52a1", style="dim")
                )
        elif event_type == "plan_replan_prompt":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(
                f"[yellow]\u26a0\ufe0f \u8ba1\u5212\u6267\u884c\u5931\u8d25 "
                f"(\u8fdb\u5ea6: {event.get('progress', '?')})[/yellow]\n"
                f"[dim]\u5931\u8d25\u539f\u56e0: {event.get('failure_reason', '?')}[/dim]\n"
                f"[yellow]\u662f\u5426\u91cd\u65b0\u89c4\u5212\u5269\u4f59\u4efb\u52a1\uff1f[/yellow]"
            )
        elif event_type == "task_text_delta":
            task_id = event.get("task_id")
            text = str(event.get("text") or "")
            if task_id and task_id in self._task_buffers:
                self._task_buffers[task_id].append(text)
        elif event_type == "task_thinking_delta":
            task_id = event.get("task_id")
            thinking = str(event.get("thinking") or "")
            if task_id and task_id in self._task_thinking_buffers:
                self._task_thinking_buffers[task_id].append(thinking)
        elif event_type == "task_tool_call":
            task_id = event.get("task_id")
            self._flush_task_thinking(task_id)
            self._flush_task_markdown(task_id)
            name = str(event.get("name") or "unknown")
            payload = event.get("input") or {}
            label = _tool_label(name, payload)
            body = Table.grid(padding=(0, 1))
            body.add_column(style="dim", no_wrap=True)
            body.add_column()
            body.add_row("task", Text(task_id or "?", style="bold #60a5fa"))
            body.add_row("tool", Text(label, style="bold #facc15"))
            body.add_row("input", Text(_format_payload(payload), style="#e5e7eb"))
            self.console.print(
                _output_panel(
                    body,
                    title=Text(
                        f"\U0001f527 Tool Use \u00b7 {task_id}",
                        style="bold #facc15",
                    ),
                    border_style="#facc15",
                )
            )
        elif event_type == "task_tool_result":
            task_id = event.get("task_id")
            self._flush_task_thinking(task_id)
            self._flush_task_markdown(task_id)
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
                    title=Text(
                        f"Tool Result \u00b7 {task_id} \u00b7 {name} \u00b7 {status}",
                        style=title_style,
                    ),
                    border_style=border_style,
                )
            )
        # -- Diff rendering --------------------------------------------
        elif event_type == "diff":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_diff(event)
        # -- Done event ------------------------------------------------
        elif event_type == "done":
            self._flush_thinking()
            # Java: answer marker before final output
            if self._buffer:
                self.console.print(Text("\u25aa", style="bold #22c55e"))
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
        # Only one Live instance per Console — stop thinking Live first
        self._stop_live_thinking()
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
                    title=Text("\U0001f9e0 \u601d\u8003\u8fc7\u7a0b", style="bold #c084fc"),
                    border_style="#6d28d9",
                )
            )

    def _update_live_thinking(self) -> None:
        if not self._live_markdown or not self.console.is_terminal:
            return
        # Only one Live instance per Console — stop text Live first
        self._stop_live_markdown()
        text = "".join(self._thinking_buffer)
        if not text.strip():
            return
        renderable = _output_panel(
            Text(text, style="dim"),
            title=Text("\U0001f9e0 \u601d\u8003\u8fc7\u7a0b", style="bold #c084fc"),
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
        cached = int(usage.get("cached_tokens") or 0)
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._cached_tokens += cached
        self._last_input_tokens = input_tokens
        self._last_output_tokens = output_tokens
        self._last_cached_tokens = cached
        self._last_total_tokens = self._input_tokens + self._output_tokens
        self._last_context_ratio = (
            self._last_input_tokens / self._context_window if self._context_window > 0 else 0
        )
        self._last_has_usage = self._last_total_tokens > 0
        self._session_cost += estimate_cost(
            self._provider,
            input_tokens,
            output_tokens,
        )
        self._last_cost = self._session_cost

    # -- Tool call rendering (Java-style labels) -------------------------

    def _print_tool_call(self, event: dict[str, Any]) -> None:
        name = str(event.get("name") or "unknown")
        payload = event.get("input") or {}
        label = _tool_label(name, payload)
        body = Table.grid(padding=(0, 1))
        body.add_column(style="dim", no_wrap=True)
        body.add_column()
        body.add_row("tool", Text(label, style="bold #facc15"))
        body.add_row("input", Text(_format_payload(payload), style="#e5e7eb"))
        self.console.print(
            _output_panel(
                body,
                title=Text("\U0001f527 Tool Use", style="bold #facc15"),
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
                title=Text(f"Tool Result \u00b7 {name} \u00b7 {status}", style=title_style),
                border_style=border_style,
            )
        )

    # -- Diff rendering (Java-style InlineDiffRenderer) -------------------

    def _print_diff(self, event: dict[str, Any]) -> None:
        file_path = str(event.get("file_path") or "")
        before = event.get("before")
        after = event.get("after")

        self.console.print(Text(f"\U0001f4dd {file_path}", style="bold cyan"))

        if before is None:
            # New file: all additions
            lines = (after or "").splitlines()
            for line in lines:
                self.console.print(Text(f"+ {line}", style="green"))
            return

        if after is None:
            # Deleted file: all removals
            lines = (before or "").splitlines()
            for line in lines:
                self.console.print(Text(f"- {line}", style="red"))
            return

        if before == after:
            self.console.print(Text("  \u5185\u5bb9\u672a\u53d8", style="dim"))
            return

        before_lines = before.splitlines()
        after_lines = after.splitlines()
        ops = _diff_ops(before_lines, after_lines)

        for op, line in ops:
            if op == "+":
                self.console.print(Text(f"+ {line}", style="green"))
            elif op == "-":
                self.console.print(Text(f"- {line}", style="red"))
            else:
                self.console.print(Text(f"  {line}", style="dim"))

    # -- Plan table (Java-style) ------------------------------------------

    def _print_plan(self, event: dict[str, Any]) -> None:
        table = Table(
            title="\U0001f4cb Execution Plan",
            title_style="bold cyan",
        )
        table.add_column("#", style="dim", no_wrap=True, width=3)
        table.add_column("ID", style="bold #60a5fa", no_wrap=True)
        table.add_column("Type", style="dim", no_wrap=True)
        table.add_column("Depends", style="dim")
        table.add_column("Task")
        for idx, task in enumerate(event.get("tasks") or [], 1):
            depends_on = ", ".join(task.get("depends_on") or [])
            table.add_row(
                str(idx),
                str(task.get("id") or ""),
                str(task.get("type") or "COMMAND"),
                depends_on or "-",
                str(task.get("description") or ""),
            )
        self.console.print(table)

    # -- Task output helpers -----------------------------------------------

    def _flush_task_output(self, task_id: str | None) -> None:
        if not task_id:
            return
        self._flush_task_thinking(task_id)
        self._flush_task_markdown(task_id)
        self._task_buffers.pop(task_id, None)
        self._task_thinking_buffers.pop(task_id, None)

    def _flush_task_markdown(self, task_id: str | None) -> None:
        if not task_id or task_id not in self._task_buffers:
            return
        buffer = self._task_buffers[task_id]
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            self.console.print(
                _output_panel(
                    Markdown(text),
                    title=Text(
                        f"\U0001f916 \u4efb\u52a1\u8f93\u51fa \u00b7 {task_id}",
                        style="bold #a8ff60",
                    ),
                    border_style="#3f3f46",
                )
            )

    def _flush_task_thinking(self, task_id: str | None) -> None:
        if not task_id or task_id not in self._task_thinking_buffers:
            return
        buffer = self._task_thinking_buffers[task_id]
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            self.console.print(
                _output_panel(
                    Text(text, style="dim"),
                    title=Text(
                        f"\U0001f9e0 \u4efb\u52a1\u601d\u8003 \u00b7 {task_id}",
                        style="bold #c084fc",
                    ),
                    border_style="#6d28d9",
                )
            )

    # -- Run summary and cost tracking ------------------------------------

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
        # Compute elapsed time
        if self._run_start_time is not None:
            self._last_elapsed = time.monotonic() - self._run_start_time
            self._run_start_time = None
        # Compute cost
        self._last_cost = self._session_cost
        self._phase = "idle"

    # -- Banner panels -----------------------------------------------------

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

    def _workspace_panel(
        self,
        *,
        model: str,
        provider: str,
        cwd: str,
        tools: int,
        mcp_servers: int,
        skills: int,
        agents_files: int,
        hitl_mode: str,
    ) -> Panel:
        notes = Text()
        # Model info
        notes.append("Model ", style="dim")
        notes.append(model, style="bold cyan")
        notes.append(f" ({provider})", style="dim")
        notes.append("\n")
        # HITL status
        if hitl_mode == "never":
            notes.append("HITL ", style="dim")
            notes.append("YOLO", style="bold yellow")
            notes.append(" Ctrl+Y to enable HITL", style="dim")
        else:
            notes.append("HITL ", style="dim")
            notes.append(hitl_mode.upper(), style="bold green")
            notes.append(" Ctrl+Y for YOLO", style="dim")
        notes.append("\n")
        # Environment summary
        env_parts: list[str] = []
        if tools:
            env_parts.append(f"Tools {tools}")
        if mcp_servers:
            env_parts.append(f"MCP {mcp_servers}")
        if skills:
            env_parts.append(f"Skills {skills}")
        if agents_files:
            env_parts.append(f"AGENTS {agents_files}")
        if env_parts:
            notes.append(" \u00b7 ".join(env_parts), style="dim")
            notes.append("\n")
        # CWD
        notes.append(_shorten_home(cwd), style="dim")
        notes.append("\n\n")
        notes.append("/help", style="purple")
        notes.append(" for commands", style="dim")
        return Panel(
            notes,
            title=Text("Workspace", style="bold green"),
            border_style="grey37",
            box=box.ROUNDED,
            padding=(0, 2),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _output_panel(renderable: Any, *, title: Text, border_style: str) -> Panel:
    return Panel(
        renderable,
        title=title,
        border_style=border_style,
        box=box.ROUNDED,
        expand=True,
    )
