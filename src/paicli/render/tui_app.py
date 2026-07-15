"""PaiCLI Textual TUI application.

Replaces the Rich + prompt_toolkit REPL loop with a full Textual app that
provides interactive, mouse-driven collapsible tool results.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, TextArea

from paicli.context.telemetry import ContextUsageState, rounded_context_percent
from paicli.render.textual_widgets import (
    ChatLog,
    InputBar,
    StartupBanner,
    StatusBar,
    format_cost,
    format_elapsed,
    format_tokens,
)
from paicli.render.tui_dialogs import ApprovalScreen, PlanReviewScreen
from paicli.render.tui_events import UiEvent


def _format_pressure_tier(tier: object) -> str:
    return {
        "tier0_observe": "T0",
        "tier1_snip": "T1",
        "tier2_prune": "T2",
        "tier3_summary": "T3",
    }.get(str(tier), "—")


class PaiCliApp(App):
    """Main PaiCLI Textual application."""

    TITLE = "PaiCLI"
    AUTO_FOCUS = "#input-bar TextArea"
    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        Binding("ctrl+y", "toggle_hitl", "Toggle HITL", show=True, priority=True),
    ]

    def __init__(
        self,
        *,
        agent: Any = None,
        config: Any = None,
        cwd: str = ".",
        registry: Any = None,
        mcp_manager: Any = None,
        console: Any = None,
        handle_slash: Any = None,
        approval_callback: Any = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self.cwd = cwd
        self.registry = registry
        self.mcp_manager = mcp_manager
        self._handle_slash = handle_slash
        self._approval_callback = approval_callback
        # State
        self._text_buffer: list[str] = []
        self._thinking_buffer: list[str] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._last_input_tokens = 0
        self._last_output_tokens = 0
        self._last_cached_tokens = 0
        self._last_total_tokens = 0
        self._last_context_ratio = 0.0
        self._last_has_usage = False
        self._pressure_tier: str | None = None
        self._pressure_ratio: float | None = None
        self._pressure_estimated = False
        self._run_start_time: float | None = None
        self._last_elapsed: float = 0.0
        self._last_cost: float = 0.0
        self._session_cost: float = 0.0
        self._provider = ""
        self._phase = "idle"
        self._context_window = 0
        self._model = ""
        self._agent_running = False
        self._worker = None  # Reference to current agent worker for cancellation
        self._task_buffers: dict[str, list[str]] = {}
        self._task_thinking_buffers: dict[str, list[str]] = {}
        self._context_usage = ContextUsageState()

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat-log")
        yield StatusBar(id="status-bar")
        yield InputBar(id="input-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"PaiCLI — {self.cwd}"
        if self.agent:
            build_context_event = getattr(self.agent, "context_usage_event", None)
            context_event = build_context_event() if callable(build_context_event) else None
            if context_event:
                self._context_usage.apply(context_event)
        self._update_status_bar()
        self._show_banner()
        self.call_after_refresh(self.query_one(TextArea).focus)

    def _show_banner(self) -> None:
        """Display a startup banner in the chat log."""
        from paicli.render._common import shorten_home as _shorten_home

        chat_log = self.query_one("#chat-log", ChatLog)
        version = "0.1.0"
        model = self._model or (self.config.llm.model if self.config else "unknown")
        provider = self._provider or (self.config.llm.provider if self.config else "unknown")
        hitl_text = self._hitl_banner_text()
        counts = self._startup_capability_counts()
        chat_log.mount(
            StartupBanner(
                version=version,
                model=model,
                provider=provider,
                hitl=hitl_text,
                tools=counts["tools"],
                skills=counts["skills"],
                mcp_servers=counts["mcp_servers"],
                cwd=_shorten_home(self.cwd),
            )
        )

    def _hitl_banner_text(self) -> str:
        mode = self.config.policy.hitl_mode if self.config else "auto"
        if mode == "never":
            return "HITL YOLO (Ctrl+Y to enable)"
        return f"HITL {mode.upper()} (Ctrl+Y for YOLO)"

    def _refresh_hitl_banner(self) -> None:
        self.query_one(StartupBanner).update_hitl(self._hitl_banner_text())

    def _startup_capability_counts(self) -> dict[str, int]:
        """Return independently reported capability totals for the startup banner."""
        tool_names = self.registry.list_names() if self.registry else []
        tools = sum(not name.startswith("mcp__") for name in tool_names)

        from paicli.skill import SkillRegistry

        skills = len(SkillRegistry(self.cwd).list())
        specs = getattr(self.mcp_manager, "specs", {}).values() if self.mcp_manager else []
        mcp_servers = sum(bool(getattr(spec, "enabled", False)) for spec in specs)
        return {"tools": tools, "skills": skills, "mcp_servers": mcp_servers}

    def action_submit_message(self) -> None:
        """Fallback action for callers that submit through the application."""
        self._submit_message(self.query_one(TextArea).text)

    def _submit_message(self, raw_message: str) -> None:
        """Route one normalized user submission to a slash command or agent run."""
        message = raw_message.strip()
        input_area = self.query_one(TextArea)
        input_area.clear()
        if not message:
            return
        if self._agent_running:
            return
        if message.startswith("/"):
            self._handle_slash_command(message)
        else:
            self.run_agent_task(message)

    def _handle_slash_command(self, raw: str) -> None:
        """Handle slash commands in the TUI."""
        chat_log = self.query_one("#chat-log", ChatLog)
        command, _, rest = raw.partition(" ")
        arg = rest.strip()

        if command in {"/exit", "/quit"}:
            self.exit()
            return
        if command == "/help":
            chat_log.add_info(self._help_text())
            return
        if command == "/clear":
            chat_log.clear_conversation()
            return
        if command == "/reset":
            if self.agent:
                self.agent.clear_history()
                build_context_event = getattr(self.agent, "context_usage_event", None)
                context_event = build_context_event() if callable(build_context_event) else None
                if context_event:
                    self._context_usage.apply(context_event)
            chat_log.clear_conversation()
            self._update_status_bar()
            return
        if command == "/context":
            self._show_context(chat_log)
            return
        if command == "/tools":
            if self.registry:
                chat_log.add_info("\n".join(self.registry.list_names()))
            return
        if command == "/config":
            from paicli.config import config_to_public_dict

            chat_log.add_info(
                json.dumps(config_to_public_dict(self.config), ensure_ascii=False, indent=2)
            )
            return
        if command == "/model":
            self._model_command(arg, chat_log)
            return
        if command == "/hitl":
            self._hitl_command(arg, chat_log)
            return
        if command == "/memory":
            self._memory_command(arg, chat_log)
            return
        if command == "/save":
            self.run_worker(self._save_command_async(arg, chat_log))
            return
        if command == "/plan":
            if not arg:
                chat_log.add_info("[red]Usage:[/red] /plan <task>")
            else:
                self.run_plan_task(arg)
            return
        if command == "/team":
            if not arg:
                chat_log.add_info("[red]Usage:[/red] /team <task>")
            else:
                self.run_agent_task(
                    "Act as planner, worker, and reviewer. "
                    "Execute this task and review the result:\n" + arg,
                )
            return
        if command == "/index":
            from paicli.rag import CodeIndex

            count = CodeIndex(self.cwd).rebuild(arg or ".")
            chat_log.add_info(f"Indexed {count} code lines.")
            return
        if command == "/search":
            from paicli.rag import CodeIndex

            results = CodeIndex(self.cwd).search(arg, limit=20)
            output = "\n".join(f"{r.path}:{r.line}: {r.snippet}" for r in results)
            chat_log.add_info(output or "(no matches)")
            return
        if command == "/mcp":
            self._mcp_command_info(arg, chat_log)
            return
        if command == "/browser":
            self._browser_command_info(arg, chat_log)
            return
        if command == "/task":
            self._task_command_info(arg, chat_log)
            return
        if command == "/snapshot":
            self._snapshot_command_info(arg, chat_log)
            return
        if command == "/restore":
            if not arg:
                chat_log.add_info("[red]Usage:[/red] /restore <snapshot-id-or-index>")
            else:
                from paicli.snapshot import SnapshotService

                record = SnapshotService(self.cwd).restore(arg)
                chat_log.add_info(f"Restored {record.id}")
            return
        if command == "/policy":
            from paicli.config import config_to_public_dict

            chat_log.add_info(
                json.dumps(
                    config_to_public_dict(self.config)["policy"], ensure_ascii=False, indent=2
                )
            )
            return
        if command == "/audit":
            limit = int(arg or "20") if (arg or "20").isdigit() else 20
            from paicli.policy import AuditLog

            chat_log.add_info(
                json.dumps(
                    AuditLog(self.config.policy.audit_log_path).tail(limit),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if command == "/skill":
            self._skill_command_info(arg, chat_log)
            return

        chat_log.add_info(f"[red]Unknown command:[/red] {command}")

    def run_agent_task(self, message: str) -> None:
        """Launch the agent as a background task."""
        self._agent_running = True
        self._phase = "running"
        self._run_start_time = time.monotonic()
        self._text_buffer.clear()
        self._thinking_buffer.clear()
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._task_buffers.clear()
        self._task_thinking_buffers.clear()
        self._update_status_bar()

        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.add_user_message(message)

        async def _run() -> None:
            try:
                if self.agent is None:
                    chat_log.add_info("[red]Agent not initialized[/red]")
                    return
                async for event in self.agent.run(message):
                    self.handle_event(event)
                    if event.get("type") == "error":
                        break
            except Exception as exc:
                chat_log.add_info(f"[bold red]Error:[/bold red] {exc}")
            finally:
                self._agent_running = False
                self._phase = "idle"
                self._worker = None
                self._update_status_bar()

        self._worker = self.run_worker(_run(), exclusive=True)

    def handle_event(self, event: dict[str, Any]) -> None:
        """Process an agent event and update the UI."""
        ui_event = UiEvent.from_agent(event)
        event_type = ui_event.kind
        payload = ui_event.payload

        if event_type == "text_delta":
            text = str(payload.get("text") or "")
            self._text_buffer.append(text)
            if text:
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.begin_stream("assistant").append(text)
        elif event_type == "thinking_delta":
            thinking = str(payload.get("thinking") or "")
            self._thinking_buffer.append(thinking)
            if thinking:
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.begin_stream("thinking").append(thinking)
        elif event_type == "usage":
            self._record_usage(payload.get("usage") or {})
        elif event_type in {
            "context_usage",
            "context_request_finished",
            "context_pending_clear",
            "context_scope_clear",
        }:
            self._context_usage.apply(payload)
            self._update_status_bar()
        elif event_type == "retry":
            chat_log = self.query_one("#chat-log", ChatLog)
            target = payload.get("tool_name") or payload.get("model") or ""
            chat_log.add_info(
                f"[yellow]Retrying {payload.get('scope', 'call')} {target} "
                f"({payload.get('attempt')}/{payload.get('max_retries')}) "
                f"after {float(payload.get('delay') or 0):.2f}s "
                f"[{payload.get('error_kind', 'unknown')}][/yellow]"
            )
        elif event_type == "retry_exhausted":
            chat_log = self.query_one("#chat-log", ChatLog)
            target = payload.get("tool_name") or payload.get("model") or ""
            chat_log.add_info(
                f"[bold red]Retry exhausted for {payload.get('scope', 'call')} {target} "
                f"after {payload.get('attempt')} retries "
                f"[{payload.get('error_kind', 'unknown')}][/bold red]"
            )
        elif event_type == "context_status":
            self._pressure_tier = payload.get("pressure_tier")
            ratio = payload.get("pressure_ratio")
            self._pressure_ratio = float(ratio) if ratio is not None else None
            self._pressure_estimated = bool(payload.get("estimated"))
            self._update_status_bar()
        elif event_type == "context_reduced":
            before = round(float(payload.get("before_ratio") or 0) * 100)
            after = round(float(payload.get("after_ratio") or 0) * 100)
            actions = ", ".join(
                str(action).replace("_", " ") for action in payload.get("actions") or []
            )
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(f"Context reduced: {before}% → {after}% · {actions}")
        elif event_type == "turn_complete":
            self._flush_thinking()
            stop_reason = str(payload.get("stop_reason") or "end_turn")
            if stop_reason != "tool_use" and self._text_buffer:
                self._flush_text("Final Output")
            elif stop_reason == "tool_use":
                self._flush_text("Assistant Output")
        elif event_type == "tool_call":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._handle_tool_call(payload)
        elif event_type == "tool_result":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._handle_tool_result(payload)
        elif event_type == "error":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(f"[bold red]Error:[/bold red] {payload.get('error')}")
        elif event_type == "done":
            self._flush_thinking()
            if self._text_buffer:
                self._flush_text("Final Output")
            self._record_run_summary(payload)
        # Plan events
        elif event_type == "plan_generation_started":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._phase = "plan"
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                "[bold cyan]\U0001f4cb \u4f7f\u7528 Plan-and-Execute \u6a21\u5f0f[/bold cyan]"
            )
            chat_log.add_info(f"  \u6b63\u5728\u89c4\u5212\u4efb\u52a1: {payload.get('goal')}")
        elif event_type == "plan_thinking":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            thinking = str(payload.get("thinking") or "")
            if thinking.strip():
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.add_thinking(thinking)
        elif event_type == "plan_review_summary":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(str(payload.get("summary") or ""))
        elif event_type == "plan_review_instructions":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                "[dim]\u8ba1\u5212\u5df2\u751f\u6210\u3002[/dim]\n"
                "  [bold]Enter[/bold]  \u6309\u5f53\u524d\u8ba1\u5212\u6267\u884c\n"
                "  [bold]Ctrl+O[/bold] \u5c55\u5f00\u5b8c\u6574\u8ba1\u5212\n"
                "  [bold]ESC[/bold]    \u6298\u53e0\u6216\u53d6\u6d88\u672c\u6b21\u8ba1\u5212\n"
                "  [bold]I[/bold]      \u8f93\u5165\u8865\u5145\u8981\u6c42\u540e\u91cd\u65b0\u89c4\u5212"
            )
        elif event_type == "plan_cancelled":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._phase = "idle"
            self._record_run_summary({})
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                "[yellow]\u23f9\ufe0f \u5df2\u53d6\u6d88\u672c\u6b21\u8ba1\u5212\u6267\u884c\u3002[/yellow]"
            )
        elif event_type == "plan_started":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._phase = "plan"
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                "[bold green]\U0001f680 \u5f00\u59cb\u6267\u884c\u8ba1\u5212...[/bold green]"
            )
        elif event_type == "plan_aggregate_result":
            chat_log = self.query_one("#chat-log", ChatLog)
            completed = payload.get("completed") or {}
            failed = payload.get("failed") or {}
            blocked = payload.get("blocked") or {}
            chat_log.add_info(
                f"[bold cyan]Execution aggregate:[/bold cyan] "
                f"status={payload.get('status')} attempts={payload.get('attempts')} "
                f"completed={len(completed)} failed={len(failed)} "
                f"blocked={len(blocked)}"
            )
        elif event_type == "plan_completed":
            self._phase = "idle"
            self._record_run_summary({})
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                "[bold green]\n\u2705 \u8ba1\u5212\u6267\u884c\u5b8c\u6210\uff01[/bold green]"
            )
            results = payload.get("results") or {}
            if results:
                chat_log.add_info(
                    f"  [dim]\u5171\u5b8c\u6210 {len(results)} \u4e2a\u4efb\u52a1[/dim]"
                )
        elif event_type == "plan_failed":
            self._phase = "idle"
            self._record_run_summary({})
            detail = payload.get("error") or payload.get("failed")
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(f"[bold red]\u274c \u8ba1\u5212\u5931\u8d25:[/bold red] {detail}")
            results = payload.get("results") or {}
            if results:
                chat_log.add_info(f"[dim]部分完成: {len(results)} 个任务[/dim]")
        elif event_type == "plan_visualization":
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(str(payload.get("visualization") or ""))
        elif event_type == "plan_replan_prompt":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                f"[yellow]\u26a0\ufe0f \u8ba1\u5212\u6267\u884c\u5931\u8d25 "
                f"(\u8fdb\u5ea6: {payload.get('progress', '?')})[/yellow]\n"
                f"[dim]\u5931\u8d25\u539f\u56e0: {payload.get('failure_reason', '?')}[/dim]\n"
                f"[yellow]\u662f\u5426\u91cd\u65b0\u89c4\u5212\u5269\u4f59\u4efb\u52a1\uff1f[/yellow]"
            )
        # Task events
        elif event_type == "task_started":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            task = payload.get("task") or {}
            task_id = ui_event.task_id or "?"
            task_type = task.get("type", "COMMAND")
            self._task_buffers[task_id] = []
            self._task_thinking_buffers[task_id] = []
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                f"\u25b6\ufe0f [bold #60a5fa]\u6267\u884c\u4efb\u52a1:[/bold #60a5fa] "
                f"{task_id} [dim][{task_type}][/dim]"
            )
        elif event_type == "task_completed":
            task_id = ui_event.task_id
            duration = payload.get("duration")
            duration_str = f" ({format_elapsed(duration)})" if duration else ""
            self._flush_task_output(task_id)
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(f"\u2705 [green]\u5b8c\u6210[/green] {task_id}{duration_str}")
        elif event_type == "task_failed":
            task_id = ui_event.task_id
            self._flush_task_output(task_id)
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                f"\u274c [red]\u4efb\u52a1\u5931\u8d25:[/red] {task_id} {payload.get('error')}"
            )
        elif event_type == "task_skipped":
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                f"\u23ed\ufe0f [yellow]\u4efb\u52a1\u8df3\u8fc7:[/yellow] {ui_event.task_id}"
            )
        elif event_type == "task_blocked":
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info(
                f"⛔ [yellow]任务阻塞:[/yellow] {ui_event.task_id} "
                f"依赖={payload.get('dependencies') or []}"
            )
        elif event_type == "task_text_delta":
            task_id = ui_event.task_id
            text = str(payload.get("text") or "")
            if task_id and task_id in self._task_buffers:
                self._task_buffers[task_id].append(text)
                if text:
                    chat_log = self.query_one("#chat-log", ChatLog)
                    chat_log.begin_stream("assistant", task_id=task_id).append(text)
        elif event_type == "task_thinking_delta":
            task_id = ui_event.task_id
            thinking = str(payload.get("thinking") or "")
            if task_id and task_id in self._task_thinking_buffers:
                self._task_thinking_buffers[task_id].append(thinking)
                if thinking:
                    chat_log = self.query_one("#chat-log", ChatLog)
                    chat_log.begin_stream("thinking", task_id=task_id).append(thinking)
        elif event_type == "task_tool_call":
            task_id = ui_event.task_id
            self._flush_task_thinking(task_id)
            self._flush_task_markdown(task_id)
            self._handle_tool_call(payload, task_id=task_id)
        elif event_type == "task_tool_result":
            task_id = ui_event.task_id
            self._flush_task_thinking(task_id)
            self._flush_task_markdown(task_id)
            self._handle_tool_result(payload, task_id=task_id)
        # Diff events
        elif event_type == "diff":
            self._flush_thinking()
            self._flush_text("Assistant Output")
            self._handle_diff(payload)

        self._update_status_bar()

    # -- Internal helpers -------------------------------------------------

    def _handle_tool_call(self, event: dict[str, Any], *, task_id: str | None = None) -> None:
        name = str(event.get("name") or "unknown")
        payload = event.get("input") or {}
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.add_tool_call(name, payload, task_id=task_id)

    def _handle_tool_result(self, event: dict[str, Any], *, task_id: str | None = None) -> None:
        is_error = bool(event.get("is_error"))
        name = str(event.get("name") or "unknown")
        result = str(event.get("result") or "")
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.finish_tool_card(name, result, is_error=is_error, task_id=task_id)

    def _handle_diff(self, event: dict[str, Any]) -> None:
        from paicli.render._common import diff_ops as _diff_ops

        file_path = str(event.get("file_path") or "")
        before = event.get("before")
        after = event.get("after")
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.add_info(f"\U0001f4dd {file_path}", style="bold cyan")
        if before is None:
            # New file: all additions
            lines = (after or "").splitlines()
            for line in lines:
                chat_log.add_info(f"+ {line}", style="green")
        elif after is None:
            # Deleted file: all removals
            lines = (before or "").splitlines()
            for line in lines:
                chat_log.add_info(f"- {line}", style="red")
        elif before == after:
            chat_log.add_info("  \u5185\u5bb9\u672a\u53d8", style="dim")
        else:
            # LCS diff with context lines
            before_lines = before.splitlines()
            after_lines = after.splitlines()
            ops = _diff_ops(before_lines, after_lines)
            for op, line in ops:
                if op == "+":
                    chat_log.add_info(f"+ {line}", style="green")
                elif op == "-":
                    chat_log.add_info(f"- {line}", style="red")
                else:
                    chat_log.add_info(f"  {line}", style="dim")

    def _flush_text(self, title: str = "Assistant Output") -> None:
        del title
        if not self._text_buffer:
            return
        self._text_buffer.clear()
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.finish_stream("assistant")

    def _flush_thinking(self) -> None:
        if not self._thinking_buffer:
            return
        self._thinking_buffer.clear()
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.finish_stream("thinking")

    def _flush_task_markdown(self, task_id: str | None) -> None:
        if not task_id or task_id not in self._task_buffers:
            return
        buf = self._task_buffers[task_id]
        if not buf:
            return
        self._task_buffers[task_id] = []
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.finish_stream("assistant", task_id=task_id)

    def _flush_task_thinking(self, task_id: str | None) -> None:
        if not task_id or task_id not in self._task_thinking_buffers:
            return
        buf = self._task_thinking_buffers[task_id]
        if not buf:
            return
        self._task_thinking_buffers[task_id] = []
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.finish_stream("thinking", task_id=task_id)

    def _flush_task_output(self, task_id: str | None) -> None:
        self._flush_task_thinking(task_id)
        self._flush_task_markdown(task_id)

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
        if self._provider:
            from paicli.render._common import estimate_cost

            self._session_cost += estimate_cost(
                self._provider,
                input_tokens,
                output_tokens,
            )
            self._last_cost = self._session_cost

    def _record_run_summary(self, event: dict[str, Any]) -> None:
        # The "done" event from query.py has top-level keys:
        #   {"type": "done", "total_tokens": ..., "total_turns": ...}
        # There is NO nested "usage" sub-key.
        total_tokens = int(event.get("total_tokens") or self._input_tokens + self._output_tokens)
        has_usage = total_tokens > 0 or self._input_tokens > 0 or self._output_tokens > 0
        context_ratio = (
            self._last_input_tokens / self._context_window if self._context_window > 0 else 0
        )
        self._last_total_tokens = total_tokens
        self._last_context_ratio = context_ratio
        self._last_has_usage = has_usage
        if self._run_start_time:
            self._last_elapsed = time.monotonic() - self._run_start_time
            self._run_start_time = None
        if self._provider:
            self._last_cost = self._session_cost

    def _update_status_bar(self) -> None:
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.model = self._model
        status_bar.phase = self._phase

        reading = self._context_usage.current
        if reading is not None:
            reading_state = str(reading.get("state") or "")
            used_tokens = int(reading.get("used_tokens") or 0)
            context_window = reading.get("context_window")
            estimate_marker = "~" if reading.get("estimated") else ""
            prefix = "ctx max " if self._context_usage.active_count > 1 else "ctx "
            if context_window:
                ratio = used_tokens / int(context_window)
                if ratio >= 0.95:
                    status_bar.context_level = "red"
                elif ratio >= 0.80:
                    status_bar.context_level = "orange"
                elif ratio >= 0.60:
                    status_bar.context_level = "yellow"
                else:
                    status_bar.context_level = "normal"
                context_text = (
                    f"{prefix}{estimate_marker}{format_tokens(used_tokens)}/"
                    f"{format_tokens(int(context_window))} "
                    f"({rounded_context_percent(ratio)}%)"
                )
            else:
                status_bar.context_level = "neutral"
                context_text = f"{prefix}{estimate_marker}{format_tokens(used_tokens)}/?"
            if self._context_usage.active_count > 1:
                context_text += f" · {self._context_usage.active_count} active"
        else:
            status_bar.context_level = "neutral"
            has_usage = self._last_has_usage
            context_ratio = self._last_context_ratio
            used_tokens = self._last_input_tokens if has_usage else 0
            context_percent = (
                f"{rounded_context_percent(context_ratio)}%" if has_usage else "0%"
            )
            if self._context_window > 0:
                context_text = (
                    f"ctx {format_tokens(used_tokens)}/"
                    f"{format_tokens(self._context_window)} ({context_percent})"
                )
            else:
                context_text = f"ctx {context_percent}"
        status_bar.context_text = context_text
        pressure_ratio = (
            reading.get("pressure_ratio") if reading is not None else self._pressure_ratio
        )
        if pressure_ratio is None:
            status_bar.pressure_text = "pressure —"
        else:
            pressure_marker = (
                "~"
                if (
                    (reading is not None and reading.get("estimated"))
                    or (reading is None and self._pressure_estimated)
                )
                else ""
            )
            status_bar.pressure_text = (
                f"pressure {pressure_marker}"
                f"{rounded_context_percent(float(pressure_ratio))}%"
            )

        token_detail = ""
        reading_state = str(reading.get("state") or "") if reading is not None else ""
        if reading is not None and reading_state != "retained":
            in_tok = int(reading.get("input_tokens") or 0)
            out_tok = int(reading.get("output_tokens") or 0)
            cached = int(reading.get("cached_tokens") or 0)
            estimate_marker = "~" if reading.get("estimated") else ""
            parts = [
                f"in:{estimate_marker}{format_tokens(in_tok)}",
                f"out:{estimate_marker}{format_tokens(out_tok)}",
            ]
            if cached:
                parts.append(f"cached:{format_tokens(cached)}")
            token_detail = " ".join(parts)
        elif self._last_has_usage:
            last_prefix = (
                "last " if reading_state == "retained" or self._phase == "idle" else ""
            )
            parts = [
                f"{last_prefix}in:{format_tokens(self._last_input_tokens)}",
                f"out:{format_tokens(self._last_output_tokens)}",
            ]
            if self._last_cached_tokens:
                parts.append(f"cached:{format_tokens(self._last_cached_tokens)}")
            token_detail = " ".join(parts)
        status_bar.token_detail = token_detail

        cost_text = format_cost(self._last_cost)
        status_bar.cost_text = cost_text

        elapsed = self._last_elapsed
        if self._run_start_time and self._phase in {"running", "plan"}:
            elapsed = time.monotonic() - self._run_start_time
        status_bar.elapsed_text = format_elapsed(elapsed) if elapsed else ""

    def action_clear_screen(self) -> None:
        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.clear_conversation()

    def action_toggle_hitl(self) -> None:
        """Toggle between default approval and explicit unattended mode."""
        if not self.config:
            return
        current = self.config.policy.hitl_mode
        next_mode = "auto" if current == "never" else "never"
        self.config.policy.hitl_mode = next_mode
        chat_log = self.query_one("#chat-log", ChatLog)
        self._refresh_hitl_banner()
        label = "auto" if next_mode == "auto" else "unattended"
        chat_log.add_info(f"[yellow]HITL switched to {label} mode[/yellow]")
        self._update_status_bar()

    def action_interrupt(self) -> None:
        """Cancel running agent if active; otherwise exit."""
        if self._agent_running and self._worker and self._worker.is_running:
            self._worker.cancel()
            self._agent_running = False
            self._phase = "idle"
            self._worker = None
            self._context_usage.apply({"type": "context_scope_clear", "scope": "agent"})
            chat_log = self.query_one("#chat-log", ChatLog)
            chat_log.add_info("[yellow]⚠️ Agent interrupted[/yellow]")
            input_area = self.query_one(TextArea)
            input_area.disabled = False
            input_area.focus()
            self._update_status_bar()
        else:
            self.exit()

    # -- Slash command helpers -------------------------------------------

    def _help_text(self) -> str:
        from paicli.entrypoints.repl import help_text

        return help_text()
        return "\n".join(
            [
                "可用命令：",
                "/help - 查看命令帮助",
                "/exit - 退出 PaiCLI",
                "/clear - 清空当前会话历史",
                "/context - 查看当前上下文状态",
                "/memory - 查看记忆系统状态",
                "/save <事实> - 保存项目级长期记忆",
                "/memory pending - 查看待确认的记忆变更",
                "/memory apply <id> - 确认待处理记忆变更",
                "/memory reject <id> - 拒绝待处理记忆变更",
                "/config - 查看当前配置",
                "/tools - 查看可用工具",
                "/model - 查看当前模型",
                "/model <模型名> - 切换当前模型名",
                "/plan <任务内容> - 用计划模式执行",
                "/team <任务内容> - 用多 Agent 协作执行",
                "/hitl on|off - 切换 HITL 审批",
                "/policy - 查看安全策略",
                "/audit [N] - 查看审计记录",
                "/index [path] - 索引代码库",
                "/search <查询> - 搜索本地代码索引",
                "/mcp - 查看 MCP server 状态",
                "/skill - 查看可用 Skill",
                "/browser - 查看浏览器会话状态",
                "/task - 查看后台任务列表",
                "/snapshot - 查看快照",
                "/restore <id> - 恢复到指定快照",
                "",
                "快捷键：",
                "  Enter       - 发送消息",
                "  Shift+Enter - 换行",
                "  Ctrl+C      - 中断运行中任务 / 空闲时退出",
                "  Ctrl+Q      - 立即退出",
                "  Ctrl+L      - 清屏",
                "  Up/Down     - 历史浏览",
                "  Tab         - 补全 slash 命令",
            ]
        )

    def _show_context(self, chat_log: ChatLog) -> None:
        if not self.agent:
            return
        client = self.agent.llm_client
        reported_window = getattr(client, "reported_context_window", None)
        safety_window = int(getattr(client, "max_context_window", 0) or 0)

        def format_reading(reading: dict[str, Any] | None) -> str:
            if not reading:
                return "none"
            marker = "~" if reading.get("estimated") else ""
            used = format_tokens(int(reading.get("used_tokens") or 0))
            window = reading.get("context_window")
            if not window:
                return f"{marker}{used}/?"
            ratio = int(reading.get("used_tokens") or 0) / int(window)
            return (
                f"{marker}{used}/{format_tokens(int(window))} "
                f"({rounded_context_percent(ratio)}%)"
            )

        lines = [
            f"model: {client.model_name} ({client.provider_name})",
            (
                f"model limit: {format_tokens(int(reported_window))}"
                if reported_window
                else "model limit: unknown"
            ),
            f"safety budget: {format_tokens(safety_window)}",
            f"retained: {format_reading(self._context_usage.retained)}",
            f"active outbound model requests: {self._context_usage.active_count}",
        ]
        for reading in sorted(
            self._context_usage.active.values(),
            key=lambda item: str(item.get("scope") or ""),
        ):
            lines.append(
                f"  {reading.get('scope') or 'request'}: {format_reading(reading)}"
            )
        if self._context_usage.active:
            lines.append(f"max: {format_reading(self._context_usage.current)}")

        estimator = getattr(client, "context_estimator", None)
        if estimator:
            factor = float(estimator.get_calibration_factor())
            samples = int(getattr(estimator, "sample_count", 0))
            lines.append(f"calibration: {factor:.2f} ({samples} samples)")

        manager = getattr(self.agent, "context_manager", None)
        manager_status = manager.get_status() if manager else {}
        compaction = manager_status.get("last_compaction") or {}
        compacted_items = int(compaction.get("compacted_items") or 0)
        compaction_kind = "llm" if compaction.get("used_llm") else "deterministic"
        lines.append(f"compaction: {compacted_items} items ({compaction_kind})")
        chat_log.add_info("\n".join(lines))

    def _model_command(self, arg: str, chat_log: ChatLog) -> None:
        if not arg:
            chat_log.add_info(f"{self.config.llm.model} ({self.config.llm.provider})")
            return
        parts = arg.split()
        if len(parts) > 2:
            chat_log.add_info("[red]Usage:[/red] /model <model> | /model <provider> <model>")
            return
        if len(parts) == 1:
            provider, model = self.config.llm.provider, parts[0]
        else:
            provider, model = parts
        if self._agent_running or self.agent is None:
            chat_log.add_info(
                "[yellow]Model switching is available only while the Agent is idle.[/yellow]"
            )
            return

        from paicli.config import load_llm_config_for_provider

        llm_config = load_llm_config_for_provider(self.cwd, provider, model)
        if not llm_config.api_key:
            chat_log.add_info(
                f"[red]Model switch failed:[/red] no API key configured for {provider}."
            )
            return
        client = self.agent.reconfigure_llm(llm_config)
        self._model = client.model_name
        self._provider = client.provider_name
        self._context_window = client.max_context_window
        self._context_usage = ContextUsageState()
        build_context_event = getattr(self.agent, "context_usage_event", None)
        context_event = build_context_event() if callable(build_context_event) else None
        if context_event:
            self._context_usage.apply(context_event)
        self.query_one(StartupBanner).update_model(self._model, self._provider)
        self._update_status_bar()
        chat_log.add_info(f"Model switched to {self._model} ({self._provider}).")

    def _hitl_command(self, arg: str, chat_log: ChatLog) -> None:
        if arg in {"always", "auto", "never"}:
            self.config.policy.hitl_mode = arg
        elif arg == "on":
            self.config.policy.hitl_mode = "always"
        elif arg == "off":
            self.config.policy.hitl_mode = "never"
        self._refresh_hitl_banner()
        chat_log.add_info(f"HITL mode: {self.config.policy.hitl_mode}")

    def _memory_command(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.memory import MemoryManager

        manager = MemoryManager(self.config.memory.long_term_path, project_path=self.cwd)
        sub, _, rest = arg.partition(" ")
        if sub in {"", "status"}:
            chat_log.add_info(manager.status())
            chat_log.add_info(f"Current project: {manager.project_path}")
        elif sub == "list":
            rows = manager.list(limit=50)
            output = "\n".join(f"{row.id} [{row.scope}] {row.content}" for row in rows)
            chat_log.add_info(output or "(no memories)")
        elif sub == "clear":
            count = manager.clear()
            chat_log.add_info(f"Cleared {count} memories.")
        elif sub == "search":
            rows = manager.search(rest)
            output = "\n".join(f"{row.id} [{row.scope}] {row.content}" for row in rows)
            chat_log.add_info(output or "(no matches)")
        elif sub == "delete":
            memory_id = rest.strip()
            if not memory_id:
                chat_log.add_info("[red]Usage:[/red] /memory delete <id>")
            elif manager.delete(memory_id):
                chat_log.add_info(f"Deleted memory {memory_id}.")
            else:
                chat_log.add_info(f"Memory not found: {memory_id}")
        elif sub == "pending":
            rows = manager.list_pending()
            chat_log.add_info(
                "\n".join(f"{row.id} [{row.operation}] {row.reason}" for row in rows)
                or "(no pending changes)"
            )
        elif sub == "apply":
            result = manager.apply_pending(rest.strip())
            chat_log.add_info(
                f"Applied memory change: {result}"
                if result is not None
                else "Pending change not found"
            )
        elif sub == "reject":
            chat_log.add_info(
                "Rejected pending change."
                if manager.reject_pending(rest.strip())
                else "Pending change not found"
            )

    def _save_command(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.memory import MemoryManager

        text = arg.strip()
        scope = "project"
        if text.startswith("--global "):
            text = text[len("--global ") :].strip()
            scope = "global"
        if not text:
            chat_log.add_info("[red]Usage:[/red] /save <fact>")
            return
        memory_id = MemoryManager(self.config.memory.long_term_path, project_path=self.cwd).save(
            text, scope=scope
        )
        chat_log.add_info(f"Saved memory {memory_id} ({scope})")

    async def _save_command_async(self, arg: str, chat_log: ChatLog) -> None:
        text = arg.strip()
        scope = "global" if text.startswith("--global ") else "project"
        if scope == "global":
            text = text[len("--global ") :].strip()
        if not text:
            chat_log.add_info("[red]Usage:[/red] /save <fact>")
            return
        from paicli.memory import MemoryManager

        result = await MemoryManager(
            self.config.memory.long_term_path, project_path=self.cwd
        ).save_with_classification(text, scope=scope, llm_client=self.agent.llm_client)
        if result.status == "pending":
            chat_log.add_info(f"Created pending memory change: {result.change_id}")
        elif result.status == "duplicate":
            chat_log.add_info(f"Memory already exists: {result.memory_id}")
        else:
            chat_log.add_info(f"Saved memory {result.memory_id} ({scope})")

    def _mcp_command_info(self, arg: str, chat_log: ChatLog) -> None:
        if self.mcp_manager is None:
            chat_log.add_info("MCP is disabled.")
            return
        sub, _, rest = arg.partition(" ")
        name = rest.strip()
        if not sub:
            lines = []
            for row in self.mcp_manager.status():
                status = row["status"]
                if row["error"]:
                    status = f"{status}: {row['error']}"
                lines.append(f"{row['name']} [{row['type']}] {status} -> {row['target']}")
            chat_log.add_info("\n".join(lines) or "(no MCP servers)")
            return
        if sub in {"enable", "disable", "restart", "logs"} and not name:
            chat_log.add_info(f"[red]Usage:[/red] /mcp {sub} <name>")
            return
        if sub == "disable":
            if self.mcp_manager.disable(name):
                removed = self.registry.unregister_prefix(f"mcp__{name}__") if self.registry else 0
                self._refresh_context_baseline()
                chat_log.add_info(f"Disabled {name}; removed {removed} tools.")
            else:
                chat_log.add_info(f'MCP server "{name}" not found.')
            return
        # Async subcommands: enable / restart — must run in the event loop,
        # not via run_until_complete (which cannot nest inside the running loop).
        if sub in {"enable", "restart"}:
            self.run_worker(
                self._mcp_async_command(sub, name, chat_log),
                exclusive=False,
            )
            return
        if sub == "logs":
            chat_log.add_info(self.mcp_manager.logs(name))
            return
        chat_log.add_info("[red]Usage:[/red] /mcp [restart|logs|disable|enable] <name>")

    async def _mcp_async_command(self, sub: str, name: str, chat_log: ChatLog) -> None:
        """Handle async MCP subcommands (enable / restart) inside the event loop."""
        try:
            if sub == "enable":
                if not self.mcp_manager.enable(name):
                    chat_log.add_info(f'MCP server "{name}" not found.')
                    return
                if self.registry:
                    self.registry.unregister_prefix(f"mcp__{name}__")
                tools = await self.mcp_manager.load_server_tools(name)
                if self.registry:
                    self.registry.register_all(tools)
                self._refresh_context_baseline()
                chat_log.add_info(f"Enabled {name}; loaded {len(tools)} tools.")
            elif sub == "restart":
                if self.registry:
                    self.registry.unregister_prefix(f"mcp__{name}__")
                count = await self.mcp_manager.restart(name)
                tools = await self.mcp_manager.load_server_tools(name)
                if self.registry:
                    self.registry.register_all(tools)
                self._refresh_context_baseline()
                chat_log.add_info(f"Restarted {name}; loaded {len(tools) or count} tools.")
        except Exception as exc:
            chat_log.add_info(f"[red]MCP error:[/red] {exc}")

    def _refresh_context_baseline(self) -> None:
        if not self.agent:
            return
        build_event = getattr(self.agent, "refresh_context_usage_event", None)
        if not callable(build_event):
            build_event = getattr(self.agent, "context_usage_event", None)
        context_event = build_event() if callable(build_event) else None
        if context_event:
            self._context_usage.apply(context_event)
            self._update_status_bar()

    def _browser_command_info(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.browser import BrowserSession

        session = BrowserSession(self.cwd)
        sub, _, rest = arg.partition(" ")
        try:
            if sub in {"", "status"}:
                state = session.status()
            elif sub == "connect":
                port = int(rest.strip()) if rest.strip() else None
                state = session.connect(port=port)
            elif sub == "disconnect":
                state = session.disconnect()
            elif sub == "tabs":
                tabs = session.tabs()
                if not tabs:
                    chat_log.add_info("No browser tabs available.")
                    return
                for tab in tabs:
                    chat_log.add_info(f"{tab.id} {tab.title} {tab.url}")
                return
            else:
                chat_log.add_info(
                    "[red]Usage:[/red] /browser status|connect [port]|disconnect|tabs"
                )
                return
        except ValueError as exc:
            chat_log.add_info(f"[red]Browser error:[/red] {exc}")
            return
        suffix = f" ({state.browser_url})" if state.browser_url else ""
        chat_log.add_info(f"Browser mode: {state.mode}{suffix}")

    def _task_command_info(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.runtime import DurableTaskManager

        manager = DurableTaskManager(Path.home() / ".paicli" / "tasks" / "tasks.db")
        sub, _, rest = arg.partition(" ")
        if sub == "add" and rest:
            task_id = manager.add(rest)
            chat_log.add_info(f"Queued {task_id}")
        elif sub == "approve" and rest:
            task = manager.resolve_reference(rest)
            chat_log.add_info(f"Approved: {manager.approve(task.id) if task else False}")
        elif sub == "deny" and rest:
            task = manager.resolve_reference(rest)
            chat_log.add_info(f"Denied: {manager.deny(task.id) if task else False}")
        elif sub == "retry" and rest:
            task = manager.resolve_reference(rest)
            task_id = manager.retry(task.id) if task else None
            chat_log.add_info(
                f"Queued retry {task_id}" if task_id else "Only failed tasks can be retried."
            )
        elif sub == "cancel" and rest:
            task = manager.resolve_reference(rest)
            chat_log.add_info(f"Canceled: {manager.cancel(task.id) if task else False}")
        elif sub == "log" and rest:
            task = manager.resolve_reference(rest)
            if not task:
                chat_log.add_info("(task not found)")
            else:
                lines = [f"Task {task.id}: {task.status}", f"Created: {task.created_at}"]
                if task.started_at:
                    lines.append(f"Started: {task.started_at}")
                if task.finished_at:
                    lines.append(f"Finished: {task.finished_at}")
                if task.duration_seconds is not None:
                    lines.append(f"Duration: {task.duration_seconds:.2f}s")
                if task.result:
                    lines.append(f"Result: {task.result}")
                if task.error:
                    lines.append(f"Error: {task.error}")
                for approval in manager.list_approvals(task.id):
                    request = approval.to_dict()["request"]
                    lines.append(
                        f"Approval: {approval.status} {request}"
                        + (f" ({approval.decision_source})" if approval.decision_source else "")
                    )
                chat_log.add_info("\n".join(lines))
        else:
            rows = manager.list(limit=20)
            output = "\n".join(
                f"{index}. {task.status} {task.prompt[:80]}"
                for index, task in enumerate(rows, start=1)
            )
            chat_log.add_info(output or "(no tasks)")

    def _snapshot_command_info(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.snapshot import SnapshotService

        service = SnapshotService(self.cwd)
        if arg == "status":
            chat_log.add_info(str(service.status()))
            return
        if arg == "clean":
            chat_log.add_info(f"Cleaned {service.clean()} snapshots.")
            return
        rows = service.list(limit=20)
        output = "\n".join(
            f"{index}. {row.id} {row.phase} {row.created_at}" for index, row in enumerate(rows, 1)
        )
        chat_log.add_info(output or "(no snapshots)")

    def _skill_command_info(self, arg: str, chat_log: ChatLog) -> None:
        from paicli.skill import SkillRegistry

        registry = SkillRegistry(self.cwd)
        sub, _, rest = arg.partition(" ")
        if sub == "show" and rest:
            skill = registry.load(rest.strip())
            if not skill:
                chat_log.add_info(f'Skill "{rest.strip()}" not found.')
                return
            chat_log.add_info(skill.content[:12_000])
            return
        rows = registry.list()
        output = "\n".join(f"{item.name}: {item.description}" for item in rows)
        chat_log.add_info(output or "(no skills)")

    # -- Plan review and approval ----------------------------------------

    async def review_plan(
        self,
        plan: Any,
        *,
        can_replan: bool = False,
    ) -> Any:
        """Push a PlanReviewScreen and return the user's PlanReviewDecision."""
        screen = PlanReviewScreen(plan, can_replan=can_replan)
        result = await self.push_screen_wait(screen)
        return result

    async def request_approval(self, request: dict[str, Any]) -> str:
        """Push an ApprovalScreen and return the decision string."""
        screen = ApprovalScreen(request)
        result = await self.push_screen_wait(screen)
        return result

    def run_plan_task(self, message: str) -> None:
        """Launch a plan-and-execute loop via the TUI's native modal flow."""
        self._agent_running = True
        self._phase = "plan"
        self._run_start_time = time.monotonic()
        self._text_buffer.clear()
        self._thinking_buffer.clear()
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._task_buffers.clear()
        self._task_thinking_buffers.clear()
        self._update_status_bar()

        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.add_user_message(f"/plan {message}")

        async def _run() -> None:
            try:
                from paicli.entrypoints.repl import _run_plan_agent

                renderer = _tui_event_renderer(self)
                await _run_plan_agent(
                    self.agent,
                    renderer,
                    message,
                    review_input=self._tui_review_input,
                )
            except Exception as exc:
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.add_info(f"[bold red]Plan error:[/bold red] {exc}")
            finally:
                self._agent_running = False
                self._phase = "idle"
                self._worker = None
                self._update_status_bar()

        self._worker = self.run_worker(_run(), exclusive=True)

    async def _tui_review_input(
        self,
        plan: Any,
        expanded: bool,
    ) -> Any:
        """Adapter: called by _run_plan_agent to get a PlanReviewDecision."""
        decision = await self.review_plan(plan, can_replan=True)
        # If the user asked to expand/collapse, handle it via events and
        # re-present the screen with updated state.  _run_plan_agent's
        # _review_plan loop handles expand/collapse natively when
        # review_input is not set; with our adapter we must replicate here.
        while decision.action in ("expand", "collapse"):
            if decision.action == "expand":
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.add_info(str(plan.visualize()))
            elif decision.action == "collapse":
                chat_log = self.query_one("#chat-log", ChatLog)
                chat_log.add_info(str(plan.summary()))
            decision = await self.review_plan(plan, can_replan=True)
        # If supplement with empty feedback, prompt for text
        if decision.action == "supplement" and not decision.feedback.strip():
            decision = await self.review_plan(plan, can_replan=True)
        return decision


class _TuiEventRenderer:
    """Minimal renderer adapter that forwards events to PaiCliApp.handle_event."""

    def __init__(self, app: PaiCliApp) -> None:
        self._app = app
        self._context_window = 0

    def set_context_window(self, window: int) -> None:
        self._context_window = window

    def set_provider(self, provider: str) -> None:
        pass

    def start_run(self) -> None:
        pass

    def newline(self) -> None:
        pass

    def handle(self, event: dict[str, Any]) -> None:
        self._app.handle_event(event)


def _tui_event_renderer(app: PaiCliApp) -> _TuiEventRenderer:
    """Create a renderer adapter for plan execution within the TUI."""
    return _TuiEventRenderer(app)
