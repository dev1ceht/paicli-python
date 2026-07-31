"""Native Textual modal screens for plan review and tool approval.

These replace the blocking Rich/prompt_toolkit prompts that were used in the
terminal REPL with non-blocking Textual ModalScreen subclasses.  Each screen
returns a typed result via ``dismiss()``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from paicli.render.textual_widgets import status_glyph
from paicli.session.models import SessionRecord

if TYPE_CHECKING:
    from paicli.plan.executor import ExecutionPlan, PlanReviewDecision


class _SessionResumeItem(ListItem):
    def __init__(self, record: SessionRecord) -> None:
        self.session_id = record.id
        model = f"{record.provider}/{record.model}" if record.model else "model unknown"
        preview = record.latest_user_preview or "(no user messages)"
        flags = "archived" if record.archived_at else record.status
        super().__init__(
            Label(
                f"{record.title}\n"
                f"{record.message_count} messages · {record.user_turn_count} turns · "
                f"{model} · {flags}\n"
                f"{preview}"
            )
        )


class SessionResumePicker(ModalScreen[str | None]):
    """Keyboard-selectable workspace session picker."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]
    DEFAULT_CSS = """
    SessionResumePicker {
        align: center middle;
        background: $background 80%;
    }
    SessionResumePicker > Vertical {
        width: 88;
        height: 24;
        padding: 1 2;
    }
    SessionResumePicker #session-picker-title {
        height: 2;
        text-style: bold;
    }
    SessionResumePicker ListView {
        height: 1fr;
    }
    SessionResumePicker ListItem {
        height: 4;
        padding: 0 1;
    }
    SessionResumePicker #session-picker-footer {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        super().__init__()
        self.records = records

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Resume a session", id="session-picker-title")
            yield ListView(*(_SessionResumeItem(record) for record in self.records))
            yield Static("↑/↓ select · Enter resume · Esc cancel", id="session-picker-footer")

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _SessionResumeItem):
            self.dismiss(item.session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InlineApprovalRequest(Static):
    """A blocking safety decision embedded in the conversation."""

    can_focus = True
    BINDINGS = [
        Binding("y", "approve", "Approve", show=False, priority=True),
        Binding("n", "deny", "Deny", show=False, priority=True),
        Binding("a", "allow_session", "Allow Tool", show=False, priority=True),
        Binding("s", "skip", "Skip", show=False, priority=True),
        Binding("escape", "deny", "Deny", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    InlineApprovalRequest {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        background: transparent;
        border: none;
    }
    InlineApprovalRequest:focus {
        border: none;
    }
    InlineApprovalRequest .decision-title {
        height: 1;
        text-style: bold;
    }
    InlineApprovalRequest .decision-summary {
        height: auto;
    }
    InlineApprovalRequest .decision-detail {
        height: auto;
        max-height: 8;
        overflow-y: auto;
    }
    InlineApprovalRequest .decision-actions {
        width: auto;
        height: 1;
        margin-top: 1;
    }
    InlineApprovalRequest .decision-actions Button {
        width: auto;
        min-width: 0;
        height: 1;
        margin-right: 1;
        padding: 0 1;
        border: none !important;
        background: transparent !important;
        text-style: bold;
    }
    InlineApprovalRequest .decision-actions Button:hover,
    InlineApprovalRequest .decision-actions Button:focus,
    InlineApprovalRequest .decision-actions Button.-active {
        border: none !important;
        background: transparent !important;
        text-style: underline bold;
    }
    InlineApprovalRequest.resolved {
        height: 1;
        margin: 0;
    }
    InlineApprovalRequest.resolved .decision-summary,
    InlineApprovalRequest.resolved .decision-detail,
    InlineApprovalRequest.resolved .decision-actions {
        display: none;
    }
    """

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__()
        self.request = request
        self._decision: str | None = None
        self._future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    @property
    def is_resolved(self) -> bool:
        return self._decision is not None

    @property
    def plain_text(self) -> str:
        tool_name = str(self.request.get("tool_name") or "unknown")
        if self._decision:
            return f"{status_glyph('success')} {tool_name} · {self._decision}"
        return f"Approval required · {tool_name} · {self._input_summary()}"

    def _input_summary(self) -> str:
        value = self.request.get("input")
        if isinstance(value, dict):
            for key in ("path", "command", "url"):
                if value.get(key):
                    return f"{key}: {str(value[key])[:120]}"
        return str(value or "")[:120]

    def _detail_text(self) -> str:
        value = self.request.get("input")
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value or "")

    def compose(self) -> ComposeResult:
        tool_name = str(self.request.get("tool_name") or "unknown")
        danger = str(self.request.get("danger_level") or "unknown")
        yield Static(
            f"{status_glyph('plan')} Approval required · {tool_name} · {danger}",
            classes="decision-title",
        )
        yield Static(self._input_summary(), classes="decision-summary")
        yield Static(self._detail_text(), classes="decision-detail")
        with Horizontal(classes="decision-actions"):
            yield Button("Approve [Y]", name="approve", classes="approval-approve")
            yield Button("Deny [N]", name="deny", classes="approval-deny")
            yield Button(
                "Allow session [A]",
                name="allow_session",
                classes="approval-allow-session",
            )
            yield Button("Skip [S]", name="skip", classes="approval-skip")

    async def wait(self) -> str:
        return await self._future

    def _resolve(self, decision: str) -> None:
        if self._future.done():
            return
        self._decision = decision
        self._future.set_result(decision)
        self.add_class("resolved", f"decision-{decision}")
        self.query_one(".decision-title", Static).update(self.plain_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.name:
            self._resolve(event.button.name)

    def action_approve(self) -> None:
        self._resolve("approve")

    def action_deny(self) -> None:
        self._resolve("deny")

    def action_allow_session(self) -> None:
        self._resolve("allow_session")

    def action_skip(self) -> None:
        self._resolve("skip")


class InlinePlanReview(Static):
    """A blocking plan decision embedded in the conversation."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "execute", "Execute", show=False, priority=True),
        Binding("ctrl+o", "toggle_expand", "Expand", show=False, priority=True),
        Binding("i", "supplement", "Supplement", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    InlinePlanReview {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        background: transparent;
        border: none;
    }
    InlinePlanReview:focus {
        border: none;
    }
    InlinePlanReview .decision-title {
        height: 1;
        text-style: bold;
    }
    InlinePlanReview .plan-summary {
        height: auto;
    }
    InlinePlanReview .plan-detail {
        display: none;
        height: auto;
    }
    InlinePlanReview.expanded .plan-detail {
        display: block;
    }
    InlinePlanReview .plan-supplement {
        display: none;
        margin-top: 1;
    }
    InlinePlanReview.supplement-mode .plan-supplement {
        display: block;
    }
    InlinePlanReview .decision-actions {
        width: auto;
        height: 1;
        margin-top: 1;
    }
    InlinePlanReview .decision-actions Button {
        width: auto;
        min-width: 0;
        height: 1;
        margin-right: 1;
        padding: 0 1;
        border: none !important;
        background: transparent !important;
        text-style: bold;
    }
    InlinePlanReview .decision-actions Button:hover,
    InlinePlanReview .decision-actions Button:focus,
    InlinePlanReview .decision-actions Button.-active {
        border: none !important;
        background: transparent !important;
        text-style: underline bold;
    }
    InlinePlanReview .plan-execute {
        color: #a8ff60 !important;
    }
    InlinePlanReview .plan-expand {
        color: #60d8ff !important;
    }
    InlinePlanReview .plan-supplement-action {
        color: #c084fc !important;
    }
    InlinePlanReview .plan-cancel {
        color: #ff4d5a !important;
    }
    InlinePlanReview.resolved {
        height: 1;
        margin: 0;
        background: transparent;
        border: none;
    }
    InlinePlanReview.resolved .plan-summary,
    InlinePlanReview.resolved .plan-detail,
    InlinePlanReview.resolved .plan-supplement,
    InlinePlanReview.resolved .decision-actions {
        display: none;
    }
    """

    def __init__(self, plan: ExecutionPlan, *, can_replan: bool = False) -> None:
        super().__init__()
        self.plan = plan
        self.can_replan = can_replan
        self._expanded = False
        self._supplement_mode = False
        self._decision: Any = None
        self._future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    @property
    def is_resolved(self) -> bool:
        return self._decision is not None

    @property
    def plain_text(self) -> str:
        if self._decision is not None:
            return f"{status_glyph('success')} Plan · {self._decision.action}"
        return f"Plan review\n{self._summary_text()}"

    def _summary_text(self) -> str:
        lines = [self.plan.summary(), "Steps:"]
        lines.extend(f"- {task.id}: {task.description}" for task in self.plan.tasks)
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Static(f"{status_glyph('plan')} Plan review", classes="decision-title")
        yield Static(self._summary_text(), classes="plan-summary")
        yield Static(self.plan.visualize(), classes="plan-detail")
        yield Input(
            placeholder="Add a requirement and press Enter",
            name="supplement",
            classes="plan-supplement",
        )
        with Horizontal(classes="decision-actions"):
            yield Button("Execute [Enter]", name="execute", classes="plan-execute")
            yield Button("Expand [Ctrl+O]", name="expand", classes="plan-expand")
            yield Button(
                "Supplement [I]",
                name="supplement",
                classes="plan-supplement-action",
            )
            yield Button("Cancel [Esc]", name="cancel", classes="plan-cancel")

    async def wait(self) -> PlanReviewDecision:
        return await self._future

    def _resolve(self, decision: PlanReviewDecision) -> None:
        if self._future.done():
            return
        self._decision = decision
        self._future.set_result(decision)
        self.add_class("resolved")
        self.query_one(".decision-title", Static).update(self.plain_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.name
        if action == "execute":
            self.action_execute()
        elif action == "expand":
            self.action_toggle_expand()
        elif action == "supplement":
            self.action_supplement()
        elif action == "cancel":
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.name != "supplement":
            return
        text = event.value.strip()
        if text:
            from paicli.plan.executor import PlanReviewDecision

            self._resolve(PlanReviewDecision.supplement(text))

    def action_execute(self) -> None:
        if self._supplement_mode:
            field = self.query_one(".plan-supplement", Input)
            text = field.value.strip()
            if text:
                from paicli.plan.executor import PlanReviewDecision

                self._resolve(PlanReviewDecision.supplement(text))
            return
        from paicli.plan.executor import PlanReviewDecision

        self._resolve(PlanReviewDecision.execute())

    def action_toggle_expand(self) -> None:
        if self._supplement_mode:
            return
        self._expanded = not self._expanded
        self.set_class(self._expanded, "expanded")

    def action_supplement(self) -> None:
        if self._supplement_mode:
            return
        self._supplement_mode = True
        self.add_class("supplement-mode")
        self.query_one(".plan-supplement", Input).focus()

    def action_cancel(self) -> None:
        if self._supplement_mode:
            self._supplement_mode = False
            self.remove_class("supplement-mode")
            self.query_one(".plan-supplement", Input).value = ""
            self.focus()
            return
        if self._expanded:
            self._expanded = False
            self.remove_class("expanded")
            return
        from paicli.plan.executor import PlanReviewDecision

        self._resolve(PlanReviewDecision.cancel())


# ---------------------------------------------------------------------------
# PlanReviewScreen
# ---------------------------------------------------------------------------


class PlanReviewScreen(ModalScreen["PlanReviewDecision"]):
    """Interactive plan-review modal.

    Key bindings
    ------------
    * **Enter** — execute the plan as shown
    * **Ctrl+O** — toggle between summary and full visualisation
    * **I** — enter *supplement* mode (type feedback, Enter to submit)
    * **Escape** — cancel the plan (or collapse if expanded)
    """

    BINDINGS = [
        Binding("enter", "execute", "Execute", show=True),
        Binding("ctrl+o", "toggle_expand", "Expand", show=True),
        Binding("i", "supplement", "Supplement", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    PlanReviewScreen {
        align: center middle;
        background: $background 80%;
    }
    PlanReviewScreen > Vertical {
        width: 76;
        height: auto;
        max-height: 22;
        padding: 1 2;
    }
    PlanReviewScreen #plan-title {
        text-style: bold;
        margin-bottom: 1;
    }
    PlanReviewScreen #plan-body {
        color: $text;
        height: auto;
        max-height: 14;
        overflow-y: auto;
    }
    PlanReviewScreen #plan-footer {
        color: $text-muted;
        margin-top: 1;
    }
    PlanReviewScreen #plan-supplement {
        display: none;
        margin-top: 1;
    }
    PlanReviewScreen.supplement-mode #plan-supplement {
        display: block;
    }
    """

    AUTO_FOCUS = None

    def __init__(self, plan: ExecutionPlan, *, can_replan: bool = False) -> None:
        super().__init__()
        self.plan = plan
        self.can_replan = can_replan
        self._expanded = False
        self._supplement_mode = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Plan Review", id="plan-title")
            yield Static(self._body_text(), id="plan-body")
            yield Static(self._footer_text(), id="plan-footer")
            yield Input(
                placeholder="Type supplement feedback and press Enter…",
                id="plan-supplement",
            )

    # -- helpers ----------------------------------------------------------

    def _body_text(self) -> str:
        if self._expanded:
            return self.plan.visualize()
        return self.plan.summary()

    def _footer_text(self) -> str:
        mode = "EXPANDED" if self._expanded else "SUMMARY"
        return f"[{mode}]  Enter=Execute  Ctrl+O=Expand  I=Supplement  Esc=Cancel"

    def _refresh_body(self) -> None:
        body = self.query_one("#plan-body", Static)
        body.update(self._body_text())
        footer = self.query_one("#plan-footer", Static)
        footer.update(self._footer_text())

    # -- actions ----------------------------------------------------------

    def action_execute(self) -> None:
        if self._supplement_mode:
            # Submit the supplement text instead
            inp = self.query_one("#plan-supplement", Input)
            text = inp.value.strip()
            if text:
                from paicli.plan.executor import PlanReviewDecision

                self.dismiss(PlanReviewDecision.supplement(text))
            return
        from paicli.plan.executor import PlanReviewDecision

        self.dismiss(PlanReviewDecision.execute())

    def action_toggle_expand(self) -> None:
        if self._supplement_mode:
            return
        self._expanded = not self._expanded
        self._refresh_body()

    def action_supplement(self) -> None:
        if self._supplement_mode:
            return
        self._supplement_mode = True
        self.add_class("supplement-mode")
        inp = self.query_one("#plan-supplement", Input)
        inp.focus()

    def action_cancel(self) -> None:
        if self._supplement_mode:
            # Leave supplement mode without submitting
            self._supplement_mode = False
            self.remove_class("supplement-mode")
            self.query_one("#plan-supplement", Input).value = ""
            return
        if self._expanded:
            self._expanded = False
            self._refresh_body()
            return
        from paicli.plan.executor import PlanReviewDecision

        self.dismiss(PlanReviewDecision.cancel())


# ---------------------------------------------------------------------------
# ApprovalScreen
# ---------------------------------------------------------------------------


class ApprovalScreen(ModalScreen[str]):
    """Tool-approval modal shown when a tool requires HITL confirmation.

    Key bindings
    ------------
    * **y** — approve this call
    * **n** / **Escape** — deny
    * **a** — allow this exact tool for the current session
    * **s** — skip this call
    """

    BINDINGS = [
        Binding("y", "approve", "Approve", show=True),
        Binding("n", "deny", "Deny", show=True),
        Binding("a", "allow_session", "Allow Tool", show=True),
        Binding("s", "skip", "Skip", show=True),
        Binding("escape", "deny", "Deny", show=True),
    ]

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $background 80%;
    }
    ApprovalScreen > Vertical {
        width: 72;
        height: auto;
        max-height: 18;
        padding: 1 2;
    }
    ApprovalScreen #approval-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ApprovalScreen #approval-body {
        color: $text;
        height: auto;
        max-height: 10;
        overflow-y: auto;
    }
    ApprovalScreen #approval-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        tool_name = str(self.request.get("tool_name", "?"))
        danger = str(self.request.get("danger_level", "?"))
        inp = str(self.request.get("input", ""))
        with Vertical():
            yield Static(
                f"Approval required: {tool_name} ({danger})",
                id="approval-title",
            )
            yield Static(inp[:2000], id="approval-body")
            yield Static(
                "y=Approve  n=Deny  a=Allow Tool This Session  s=Skip",
                id="approval-footer",
            )

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_deny(self) -> None:
        self.dismiss("deny")

    def action_allow_session(self) -> None:
        self.dismiss("allow_session")

    def action_skip(self) -> None:
        self.dismiss("skip")
