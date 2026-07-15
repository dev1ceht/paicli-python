# Restrained Aurora TUI Design

## Status

Implemented on `codex/restrained-aurora-tui`. The Textual TUI regression suite covers the new presentation and interaction contracts.

## Goal

Make PaiCLI's Textual interface calmer, more legible, and more space-efficient while preserving its terminal-first identity, operational visibility, and existing Agent safety semantics.

The design language is **Restrained Aurora**: neutral dark surfaces carry content, while a small semantic palette communicates focus, state, and key actions. The interface should feel like a professional development tool rather than a stack of neon panels.

## Scope

This is a TUI presentation-layer redesign, not a CSS-only reskin. It includes:

- an adaptive startup banner;
- a conversation-first message hierarchy;
- a compact activity rail for thinking and tool work;
- inline approval and plan-review decisions;
- an adaptive command dock;
- removal of the persistent Textual footer;
- intelligent streaming follow behavior;
- consistent semantic colors and terminal-safe status glyphs.

Agent behavior, tool execution, safety policy, audit semantics, approval result types, and plan execution semantics remain unchanged.

## Design baseline

- Windows Terminal and PowerShell 7 remain the primary environment.
- `80x24` is the minimum viewport guaranteed to show the complete interface and all current status-bar information.
- Wider terminals may use more generous spacing and longer summaries.
- Terminals narrower than 80 columns remain operable, but text may be abbreviated or clipped.
- When height is constrained, preserve the active decision, command dock, and status bar before banner or history content.

## Visual language

### Surfaces and hierarchy

- Use neutral dark surfaces for the screen and readable gray-white for body text.
- Do not wrap every object in a full border.
- Use spacing, a single edge accent, and restrained background contrast to establish hierarchy.
- Avoid large areas of saturated color.

### Semantic colors

| Color | Meaning |
|---|---|
| Cyan | Focus, active work, interactive elements |
| Green | Success, completion, primary confirmation |
| Blue | User submissions |
| Purple | Thinking and planning |
| Yellow | Warnings, cost, and approval decisions |
| Red | Errors and high risk |
| Gray/white | Ordinary structure and readable content |

Colors are not decorative aliases. Every status also has a text label so that color is never the only signal.

### Glyphs and motion

- Use single-cell Unicode symbols such as `●`, `○`, `◆`, `✓`, and `×` when supported.
- Provide ASCII fallbacks such as `*`, `o`, `>`, `OK`, and `ERR`.
- Do not use emoji as status icons because their rendered width varies across terminals and fonts.
- Motion is limited to a subtle single-character running indicator.
- Streaming text appears immediately without artificial typewriter delays.
- Errors and approval requests never flash.

## Layout

The main screen remains a vertical composition:

1. conversation canvas;
2. adaptive command input;
3. persistent one-line operational status.

There is no separate persistent shortcut footer.

### Startup banner

- Replace the large ASCII logo with a single-line wordmark, for example `PAICLI  v0.1.0  —  Ready to build`.
- Show the active model, provider, HITL mode, capability counts, and workspace in two or three subordinate lines.
- Use about four to five lines on wide terminals and about three lines at the 80-column baseline.
- After the first user submission, recede to a one-line session summary.
- The banner must not compete with active conversation content.

### Conversation canvas

- Render assistant output as unboxed Markdown on the primary reading surface.
- Render each user submission as a compact blue-accented prompt block.
- Use small role labels instead of large repeated `You` and `Assistant Output` panel titles.
- Use content structure—paragraphs, lists, headings, and code blocks—instead of outer panels to organize long answers.
- Keep a modest separation between turns without creating card-stack spacing.

### Command dock

- Keep the existing one-line status information: phase, model, context usage, compression pressure, token details, cost, and elapsed time.
- Do not replace status information with shortcut hints.
- The message input starts at about two lines and grows with multiline input to about five lines.
- Remove the standalone `>` label.
- Use a low-contrast idle edge and a cyan focused edge.
- Preserve `Enter` to submit, `Shift+Enter` for a newline, and `Ctrl+C` to interrupt.
- Store the complete shortcut reference in `/help` instead of a persistent footer.

## Activity rail

Thinking and tool activity form one compact chronological group within the conversation.

### Thinking

- While active, show a visible purple one-line status.
- When complete, collapse it to a muted summary including elapsed time when available.
- Allow the user to expand the complete thinking text.
- Do not reserve space when the model provides no thinking content.

### Tool calls

- Represent a tool call with a one-line summary containing status, tool name, important arguments, and elapsed time.
- Use cyan while running, muted green after success, and red for failure.
- Collapse successful calls automatically.
- Keep failed calls expanded to an actionable summary, with full output available in a scrollable detail area.
- Group consecutive calls visually instead of rendering independent large cards.
- Group repeated retries of the same failure instead of creating duplicate red blocks.

## Inline decisions

### Approval requests

- Do not navigate to a separate Textual `ModalScreen`.
- Mount the approval request at the point where the tool activity pauses.
- Show the tool, danger level, affected target, and a concise input summary before raw detail.
- Temporarily lock ordinary message submission and focus the approval actions.
- Preserve the existing `approve`, `deny`, `skip`, and `allow_session` results and their keyboard shortcuts.
- After resolution, collapse the request to a compact decision and audit trace.

### Plan review

- Do not navigate to a separate Textual `ModalScreen`.
- Mount a plan-review block in the conversation context.
- Default to a goal and step summary, with the complete plan expandable in place.
- Support execute, supplement, and cancel without changing `PlanReviewDecision` semantics.
- Collect supplement text inside the review block while ordinary message submission is locked.
- After resolution, collapse the review block to its plan status and append execution activity below it.

## Errors

- Keep errors visible at the point where they occur.
- Use a red edge and concise actionable summary rather than a fully red panel.
- Keep ordinary error detail gray-white for readability.
- Put complete stdout/stderr or diagnostic output in expandable, scrollable detail.
- Keep high-risk and error semantics visible in monochrome terminals through labels and glyphs.

## Focus and input

- All actions must be operable by keyboard; mouse interaction is an enhancement.
- `Tab` and `Shift+Tab` traverse the currently actionable controls.
- A cyan edge indicates focus consistently.
- `Enter` activates the focused action and `Esc` backs out of or collapses local detail where safe.
- Existing submission, multiline, cancellation, history, and slash-completion meanings remain intact.

## Conversation follow behavior

- Automatically follow streaming output only while the user remains at the bottom of the canvas.
- Suspend follow mode when the user scrolls into history.
- Show a lightweight `New activity` affordance and support `Ctrl+End` globally (or `End` while the affordance is focused) to return to the bottom without stealing the input field's normal `End` behavior.
- Do not forcibly jump to an inline approval; show a persistent yellow attention indicator until the user returns to it.
- Resume follow mode when the user explicitly returns to the bottom.

## Backend boundary

Default to presentation-only changes in `paicli.render` and the TUI adapter in `PaiCliApp`.

The following contracts remain stable:

- Agent event meanings such as `text_delta`, `thinking_delta`, `tool_call`, `tool_result`, usage, task, and plan events;
- `ToolDecision`: `approve`, `allow_session`, `deny`, or `skip`;
- `PlanReviewDecision` actions and supplement feedback;
- policy, audit, cancellation, tool execution, and planning behavior.

If a confirmed UI requirement cannot be implemented reliably with current event data, a minimal additive backend field is allowed. It must be backward-compatible and covered by contract tests. For example, a stable `tool_call_id` could later improve correlation if same-name tool calls become concurrent; current sequential execution does not require it.

## Theme boundary

This iteration provides one default Restrained Aurora theme. Semantic colors and spacing should be centralized as reusable tokens, but theme selection, user-defined themes, and a light theme are out of scope.

## Verification

Implementation should preserve the existing TUI regression suite and add coverage for:

- 80x24 layout and banner collapse;
- unboxed assistant output and compact user prompts;
- activity grouping, completion collapse, and expanded failures;
- inline approval decisions and retained audit traces;
- inline plan review, supplement input, and decision results;
- command input growth and unchanged one-line status content;
- footer removal and focus traversal;
- follow-mode suspension and resume behavior;
- Unicode glyph fallback and semantic status labels;
- unchanged ToolDecision and PlanReviewDecision contracts.

## Non-goals

- Changing Agent reasoning, tool scheduling, or plan execution;
- changing safety, approval, or audit policy;
- adding a web interface;
- adding theme selection or user-authored themes;
- supporting full visual fidelity below 80 columns;
- restoring a Rich or prompt-toolkit fallback REPL.
