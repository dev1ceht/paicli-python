# PaiCLI

PaiCLI is a terminal AI agent that accepts user input in a Textual TUI and relays it to an OpenAI-compatible model service.

## Language

**Background task**:
A durable, independently executed agent request whose lifecycle is recorded outside an interactive session.
_Avoid_: job, async request

**Queued task**:
A background task accepted for execution but not yet exclusively claimed by a worker.
_Avoid_: pending task, enqueued task

**Exclusive task claim**:
The single successful transition of a queued task to `running` by one worker, even when workers compete concurrently.
_Avoid_: dequeue, task pickup

**Terminal task status**:
One of `completed`, `failed`, or `canceled`; a task in a terminal status cannot transition again.
_Avoid_: final state, done state

**Background-task lifecycle**:
The permitted task transitions are `queued` to `running` or `canceled`, and `running` to `completed`, `failed`, or `canceled`; invalid or stale transitions are ignored.
_Avoid_: task progress, task state flow

**Background-task cancellation**:
The irreversible transition of a background task to `canceled`; it prevents later result writes but does not guarantee interruption of an in-flight external operation.
_Avoid_: force stop, thread kill

**Cooperative task cancellation**:
The stopping of a canceled background task at the next Agent or tool execution boundary, without forcibly interrupting an in-flight operation or presenting cancellation to the Agent as a tool failure.
_Avoid_: immediate cancellation, request abort

**TUI submission**:
A non-empty message or slash command accepted by the focused PaiCLI input field after Enter is pressed.
_Avoid_: typing, draft

**Startup banner**:
The adaptive session introduction displayed throughout the PaiCLI TUI session; it presents application identity and current capabilities without shrinking after submission.
_Avoid_: splash screen, chat area

**Restrained Aurora visual language**:
PaiCLI's calm, professional terminal aesthetic: neutral dark surfaces carry content, while Aurora green, cyan, purple, yellow, and red are reserved for focus, state, and key actions.
_Avoid_: cyberpunk theme, neon-heavy UI, decorative color

**Aurora semantic colors**:
The fixed status vocabulary within the restrained Aurora visual language: cyan means focus or active work, green success, blue user input, purple reasoning or planning, yellow warning or approval, and red error or high risk.
_Avoid_: decorative accents, role-dependent recoloring, rainbow status

**Terminal-safe status glyph**:
A single-cell Unicode status symbol with a textual label and an ASCII fallback, chosen to preserve alignment across supported terminals and fonts.
_Avoid_: emoji status icon, color-only status, decorative symbol

**Conversation canvas**:
The primary reading surface where assistant output appears as unboxed content and each user submission appears as a compact, subordinate prompt block.
_Avoid_: message-card stack, chat bubbles, transcript panel

**Conversation follow mode**:
The automatic tracking of new conversation output while the user remains at the bottom of the canvas; manual history navigation suspends tracking until the user explicitly returns.
_Avoid_: forced auto-scroll, scroll lock, sticky bottom

**Activity rail**:
The compact chronological group of Agent thinking and tool activity, where active events remain visible, completed events recede to expandable summaries, and failures remain exposed.
_Avoid_: tool-card stack, execution log, debug console

**Command dock**:
The bottom interaction area combining an adaptive message input with PaiCLI's persistent one-line operational status; it excludes a separate shortcut footer.
_Avoid_: input bar, command prompt, footer stack

**Inline approval request**:
A blocking safety decision presented inside the current activity rail, retaining its resolved outcome as a compact audit trace without navigating away from the conversation canvas.
_Avoid_: approval screen, confirmation dialog, warning popup

**Inline plan review**:
A blocking plan decision presented inside the conversation canvas, where the user can inspect, supplement, execute, or cancel a plan without navigating away from its surrounding context.
_Avoid_: plan screen, plan dialog, full-screen review

**MCP server**:
A configured external Model Context Protocol service, regardless of how many capabilities it exposes to PaiCLI.
_Avoid_: MCP tool, MCP count

**MCP invocation**:
A call from PaiCLI to a capability exposed by a configured MCP server. Every MCP invocation is audit-recorded; it is approval-gated by default but is eligible for unattended mode and exact-tool session allowlisting.
_Avoid_: MCP server, remote request

**Enabled MCP server**:
An MCP server whose configuration is enabled; it contributes one unit to the startup banner's MCP count even when its connection currently has an error.
_Avoid_: available MCP server, loaded MCP tool

**Available Skill**:
A named PaiCLI skill discovered from the user or current project's skill directory, with duplicate names represented once.
_Avoid_: loaded skill, built-in tool

**Model endpoint**:
The configured OpenAI-compatible HTTP service that produces streaming agent events for a submitted message.
_Avoid_: frontend, terminal UI

**Hot model switch**:
A session-scoped change to the active model endpoint that takes effect only while the Agent is idle, so the next submitted message uses the replacement endpoint and its provider-specific connection settings.
_Avoid_: mid-run switch, live migration

**Provider-specific connection settings**:
The API key and base URL selected for a model provider from the project's environment configuration.
_Avoid_: shared credentials, endpoint defaults

**Session history**:
The accumulated conversation messages retained by an Agent across completed submissions; it remains available after a hot model switch.
_Avoid_: transcript, chat log

**Context-management evaluation**:
A paired experiment that compares PaiCLI context-reduction variants for task quality and context consumption.
_Avoid_: context test, compression test

**Context-management effectiveness**:
The demonstrated result that, on live long-session coding tasks containing task-relevant prior information, a context-reduction variant lowers provider-reported actual input-token usage relative to full history without reducing task correctness.
_Avoid_: compression works, token savings alone, scripted cost win

**Scripted context-cost evaluation**:
A context-management evaluation in which a scripted model replays fixed tool calls while PaiCLI executes them in an isolated fixture copy. Its token measurements are estimated proxies, not provider billing telemetry.
_Avoid_: pure event replay, real-cost evaluation

**Coding benchmark task**:
An isolated repository-level coding problem with a stated goal and an independent correctness check.
_Avoid_: unit test, fixture, prompt

**Benchmark fixture**:
The version-controlled starting repository for a coding benchmark task, copied into an isolated workspace for each attempt so the source remains unchanged.
_Avoid_: live workspace, task manifest, verifier

**Public benchmark test**:
A task test available inside the Agent workspace to support diagnosis and iteration; it provides development feedback but does not alone determine task correctness.
_Avoid_: acceptance test, verifier, quality gate

**Acceptance verifier**:
The authoritative correctness check executed outside the Agent workspace against the Agent's resulting change, using validation material the Agent could neither inspect nor modify during the attempt.
_Avoid_: public test, self-verification, Agent test run

**Coding benchmark attempt**:
One execution of PaiCLI against a coding benchmark task, producing a workspace change, an Agent response, execution evidence, and a verification outcome.
_Avoid_: test run, chat session, evaluation task

**Local verification benchmark**:
A coding benchmark whose task repository and correctness check run entirely in a controlled local environment, used as the first end-to-end gate for the shared coding-evaluation protocol.
_Avoid_: unit-test suite, scripted context-cost evaluation, official benchmark

**Production-path benchmark execution**:
A coding benchmark attempt that uses the same Agent orchestration and safety boundaries as normal PaiCLI use, while allowing benchmark-specific configuration and a controlled model client.
_Avoid_: benchmark Agent, test-only loop, simulated Agent

**Context-reduction variant**:
One controlled context-handling policy used for every run of the same benchmark task: full history, deterministic compaction, or LLM-handoff compaction.
_Avoid_: experiment mode, model variant

**Synthetic pressure history**:
Benchmark-only conversation history added before a scripted task to cross the context-pressure threshold. It is controlled test input, not evidence that a production task has the same history.
_Avoid_: real conversation history, task transcript

**Quality gate**:
The required threshold that a context-reduction variant must meet on task verification before its cost result is considered acceptable.
_Avoid_: performance score, token target

**Guarded finalization**:
A single model turn without tool access that follows an agent safety limit, producing a conclusion from the evidence already collected. A further tool request during this turn ends the run.
_Avoid_: graceful stop, final retry

**Repeated-call stagnation**:
Three consecutive tool batches with the same tool and normalized input, with no successful workspace write or new result in between.
_Avoid_: retry, polling

**Approval-gated operation**:
A tool invocation that must receive an explicit user decision before execution; workspace writes, snapshot restores, and all MCP invocations are approval-gated by default.
_Avoid_: dangerous operation, privileged tool

**Session tool allowlist**:
The in-memory set of explicitly approved exact tool identities that may bypass a later approval prompt for the remainder of the current session. It never grants access to another tool from the same MCP server or disables global safety policy.
_Avoid_: server allowlist, global allowlist

**Unattended mode**:
An explicitly selected session policy that suppresses approval prompts for eligible tool invocations. It does not bypass path guards, command policy, or audit logging.
_Avoid_: approve all, implicit YOLO

**Mandatory-confirmation operation**:
A destructive operation that requires an explicit approval even in unattended mode. Snapshot restoration is a mandatory-confirmation operation because it can delete or overwrite many workspace files.
_Avoid_: high-risk tool, prompt-required action

**Audit event**:
An append-only record of a tool decision or execution, containing redacted input metadata and a length-limited, redacted result summary rather than raw tool output.
_Avoid_: execution transcript, debug log

**Mandatory audit trail**:
The audit record required for every sensitive operation and every MCP invocation. It cannot be disabled by session or feature configuration.
_Avoid_: optional audit, debug telemetry

**Audit availability gate**:
The rule that a sensitive operation or MCP invocation does not execute when its mandatory audit event cannot be persisted.
_Avoid_: best-effort audit, post-hoc logging

**Snapshot link exclusion**:
The rule that snapshots omit symbolic links instead of resolving, copying, or restoring them. The snapshot reports how many links it omitted.
_Avoid_: link preservation, link traversal

**Policy preflight**:
A static safety-policy evaluation performed before an approval prompt, with the same policy evaluated again immediately before execution.
_Avoid_: advisory warning, approval check

**Execution sandbox**:
An optional operating-system-enforced environment in which command tools run with access limited to the workspace and explicitly allowed capabilities. It is distinct from approval, path guards, and command policy; those controls do not constitute a sandbox.
_Avoid_: working directory, command guard, path sandbox

**Pending memory change**:
A proposed update, merge, or retirement discovered while an explicitly requested long-term memory is being saved. It does not affect retrieval or stored memories until the user explicitly confirms it.
_Avoid_: automatic memory update, uncommitted memory

**Long-term memory**:
A user-requested persistent fact scoped to one project or all projects. It is stable background context, not an automatically extracted transcript of a conversation.
_Avoid_: conversation archive, automatic user profile

**Memory relationship**:
The classification of a requested memory against a retrieved existing memory: `duplicate` states the same fact, `merge` adds compatible detail, `replace` supersedes it, and `independent` is a separate fact.
_Avoid_: similarity score, conflict type

**Structured conversation summary**:
A compact, fixed-format account of compressed session history. It records the user goal, constraints, files read and modified, completed operations and results, key decisions, current workspace state, blockers, and next steps.
_Avoid_: transcript, free-form chat summary

**Outbound model request**:
The single, role-preserving set of instructions and conversation messages sent to a model for one Agent turn. Each user request, assistant response, and tool result appears at most once in it.
_Avoid_: assembled prompt, flattened history

**Reasoning continuity**:
The preservation of a model-produced reasoning payload between tool-calling turns when that provider requires it to continue the same inference.
_Avoid_: displayed thinking, chain-of-thought transcript
