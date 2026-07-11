"""Native Textual modal screens for plan review and tool approval.

These replace the blocking Rich/prompt_toolkit prompts that were used in the
terminal REPL with non-blocking Textual ModalScreen subclasses.  Each screen
returns a typed result via ``dismiss()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from paicli.plan.executor import ExecutionPlan, PlanReviewDecision


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
        background: #0d1117;
        border: heavy #60d8ff;
        padding: 1 2;
    }
    PlanReviewScreen #plan-title {
        text-style: bold;
        color: #60d8ff;
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
        return (
            f"[{mode}]  "
            "Enter=Execute  Ctrl+O=Expand  I=Supplement  Esc=Cancel"
        )

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
        background: #0d1117;
        border: heavy #facc15;
        padding: 1 2;
    }
    ApprovalScreen #approval-title {
        text-style: bold;
        color: #facc15;
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
