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

**Scripted benchmark client**:
A deterministic model substitute that emits predefined production-format Agent responses and tool calls to verify benchmark infrastructure without measuring model capability.
_Avoid_: mock Agent, coding model, live benchmark

**Live coding benchmark**:
A coding benchmark executed with a configured external model service to measure the end-to-end coding behavior of that PaiCLI and model combination.
_Avoid_: scripted benchmark, framework test, model-only evaluation

**Coding benchmark task**:
An isolated repository-level coding problem with a stated goal and an independent correctness check.
_Avoid_: unit test, fixture, prompt

**Benchmark task prompt**:
The versioned user message sent verbatim through the production Agent path for a coding benchmark task, without benchmark-specific wrapper instructions.
_Avoid_: system prompt, task metadata, benchmark preamble

**Benchmark manifest**:
The strictly validated, versioned definition of a benchmark suite and its tasks, containing safe references to fixtures and withheld acceptance material rather than embedded repository contents.
_Avoid_: task fixture, run artifact, JSONL dataset

**Benchmark fixture**:
The version-controlled starting repository for a coding benchmark task, copied into an isolated workspace for each attempt so the source remains unchanged.
_Avoid_: live workspace, task manifest, verifier

**Public benchmark test**:
A task test available inside the Agent workspace to support diagnosis and iteration; it provides development feedback but does not alone determine task correctness.
_Avoid_: acceptance test, verifier, quality gate

**Acceptance verifier**:
The authoritative correctness check executed outside the Agent workspace against the Agent's resulting change, using preloaded validation material whose integrity is independent of the attempt.
_Avoid_: public test, self-verification, Agent test run

**Withheld acceptance material**:
Version-controlled verifier inputs deliberately omitted from the Agent workspace and preloaded before the attempt; without filesystem isolation they are withheld by layout but not guaranteed confidential from arbitrary shell access.
_Avoid_: secret test, public benchmark test, encrypted fixture

**Acceptance integrity**:
The guarantee that final verification uses the fingerprinted acceptance material preloaded before an attempt, unaffected by files or tests the Agent later changes.
_Avoid_: test confidentiality, fixture immutability, patch validation

**Acceptance confidentiality**:
The guarantee that an Agent cannot observe withheld acceptance material; the baseline local smoke suite does not provide it because arbitrary shell execution is not filesystem-isolated.
_Avoid_: acceptance integrity, hidden-by-layout, path guard

**Coding benchmark correctness**:
The pass-or-fail outcome determined exclusively by the acceptance verifier for a completed coding benchmark attempt.
_Avoid_: composite score, Agent confidence, public-test result

**Benchmark telemetry**:
Non-authoritative measurements attached to an attempt, such as context consumption, Agent turns, elapsed time, patch size, and execution errors; telemetry supports comparison but cannot compensate for incorrectness.
_Avoid_: benchmark score, correctness, quality grade

**Benchmark token usage**:
Provider-reported input, output, and total token usage for a live attempt, kept distinct from estimated context measurements and synthetic scripted-client data.
_Avoid_: estimated cost, context size, model price

**Benchmark input-token cost**:
The provider-reported input tokens consumed by all model calls attributable to a scheduled live attempt, including context-summary calls; suite averages include resolved and unresolved attempts rather than only successful tasks.
_Avoid_: estimated context, successful-task cost, main-loop usage only

**Benchmark execution status**:
The outcome of running an attempt through PaiCLI: `completed`, `agent_error`, or `benchmark_error`; it identifies whether execution finished or which boundary prevented a fair result.
_Avoid_: correctness, test result, pass status

**Benchmark verification status**:
The acceptance verifier outcome `passed`, `failed`, or `not_run`, recorded independently from benchmark execution status.
_Avoid_: execution status, Agent result, public-test status

**Coding benchmark attempt**:
One execution of PaiCLI against a coding benchmark task, producing a workspace change, an Agent response, execution evidence, and a verification outcome.
_Avoid_: test run, chat session, evaluation task

**Benchmark patch**:
The complete net change from a task fixture's recorded baseline to the Agent workspace's final file tree, independent of the Agent's staging or commit behavior.
_Avoid_: working-tree diff, commit, verifier patch

**Benchmark repetition**:
An independent coding benchmark attempt for the same task and recorded configuration, starting from a fresh workspace and Agent session so observed variation is not inherited state.
_Avoid_: retry, resumed attempt, verifier rerun

**Benchmark tool profile**:
The named, recorded subset of production PaiCLI tools available to every attempt in a comparable benchmark run.
_Avoid_: tool registry, permission mode, Agent capability

**Network-tool-free coding profile**:
The baseline benchmark tool profile containing workspace-scoped file operations plus shell commands launched from the task workspace, while excluding dedicated web, browser, MCP, long-term memory, and snapshot restoration tools. Shell commands are neither filesystem- nor network-isolated, so this profile is not an execution sandbox.
_Avoid_: offline profile, hermetic environment, sandbox

**Unsandboxed benchmark acknowledgement**:
The explicit live-run confirmation that Agent shell commands and verifier execution use the current user's host permissions; it records informed acceptance but provides no isolation.
_Avoid_: sandbox enablement, permission grant, unattended mode

**Benchmark resource budget**:
The recorded Agent turn, tool-call, elapsed-time, and token limits shared by every task in a comparable benchmark run; changing the budget creates a different benchmark configuration.
_Avoid_: task timeout, usage telemetry, per-task allowance

**Local verification benchmark**:
A coding benchmark whose task repository and correctness check run entirely in a controlled local environment, used as the first end-to-end gate for the shared coding-evaluation protocol.
_Avoid_: unit-test suite, scripted context-cost evaluation, official benchmark

**Local smoke suite**:
The versioned `local-smoke-v2` seven-task local verification benchmark, comprising five PaiCLI long-session scenarios and two adapted FirstCoder local-pytest tasks, that exercises end-to-end coding evaluation and paired context pressure across representative small repository changes; it validates integration and comparative behavior, not broad coding mastery.
_Avoid_: capability leaderboard, official benchmark score, regression unit tests

**Frozen pressure history**:
A committed, fingerprinted sequence of task-relevant prior messages attached to a local-smoke task to reproduce normal, medium, or high context pressure without exposing withheld acceptance material.
_Avoid_: prompt padding, acceptance fixture, live session transcript

**Full-history benchmark variant**:
The control arm of a paired context-management evaluation that retains the same frozen pressure history and production Agent path while disabling context reduction through a benchmark-only context manager.
_Avoid_: old PaiCLI version, truncated baseline, different task prompt

**Optimized-context benchmark variant**:
The treatment arm of a paired context-management evaluation that processes the same frozen pressure history through PaiCLI's production `ContextManager` and its unchanged pressure thresholds.
_Avoid_: rewritten task, synthetic compression, benchmark-only reducer

**Benchmark suite identity**:
The combination of a suite's versioned name and content fingerprint; results are directly comparable only when both values match.
_Avoid_: display name, runner version, Git branch

**Benchmark runtime identity**:
The recorded PaiCLI version, source revision, dirty state, and relevant source-content fingerprint that identify the implementation exercised by a benchmark run.
_Avoid_: model identity, suite identity, run ID

**Benchmark configuration identity**:
The secret-free fingerprint of model settings, resource budget, and benchmark tool profile used for a run.
_Avoid_: API credentials, runtime identity, task manifest

**Benchmark environment identity**:
The recorded operating system, architecture, Python version, and relevant dependency versions of the environment hosting a benchmark run.
_Avoid_: runtime identity, model configuration, task workspace

**Low-variance benchmark sampling**:
The baseline live-benchmark sampling profile that requests model temperature zero to reduce response variation without claiming deterministic model behavior.
_Avoid_: deterministic model, fixed seed, production sampling default

**Benchmark replicate**:
A run whose suite, runtime, benchmark configuration, and environment identities match another run, allowing their attempts to be aggregated as repeated samples.
_Avoid_: rerun, historical result, controlled comparison

**Controlled benchmark comparison**:
A comparison using the same suite identity in which one declared dimension, such as PaiCLI runtime or model, changes while all non-target dimensions remain fixed and recorded.
_Avoid_: result aggregation, side-by-side report, arbitrary comparison

**Benchmark run artifact**:
A redacted, reconstructable record of one benchmark run containing structured results and per-attempt evidence, stored outside version control without retained workspaces by default.
_Avoid_: benchmark definition, source fixture, session archive

**Formal benchmark run**:
An immutable benchmark run intended to support an external comparison or resume claim, executed from a clean runtime and finalizable only when every scheduled attempt and required official outcome is complete and valid.
_Avoid_: development run, partial result, selected attempts

**Production-path benchmark execution**:
A coding benchmark attempt that uses the same Agent orchestration and safety boundaries as normal PaiCLI use, while allowing benchmark-specific configuration and a controlled model client.
_Avoid_: benchmark Agent, test-only loop, simulated Agent

**Frozen generation attempt**:
An attempt whose model response, provider usage, patch, and safe event evidence have been atomically persisted and hashed, allowing deterministic post-generation checks to resume without another model sample.
_Avoid_: completed attempt, model checkpoint, retryable model call

**Patch status**:
The generation-artifact classification `non_empty`, `empty`, or `credential_blocked`, recorded separately from Agent execution state and official resolved outcome.
_Avoid_: pass/fail, termination reason, apply-check result

**Termination reason**:
The reason an Agent loop ended, such as natural completion, a declared resource guard, context-limit failure, interruption, or credential blocking; it is independent from patch presence and official harness correctness.
_Avoid_: attempt state, patch status, official outcome

**Context-stress profile**:
A versioned evaluation-only configuration that gives full-history and optimized context variants the same explicit input budget; any budget change creates a different immutable profile identity.
_Avoid_: production context budget, model context window, natural long-context workload

**Context-reduction variant**:
One controlled context-handling policy used for every run of the same benchmark task: full history, deterministic compaction, or LLM-handoff compaction.
_Avoid_: experiment mode, model variant

**Full-history benchmark baseline**:
The context-reduction variant that preserves outbound conversation and tool-result history without reduction while retaining the same Agent runtime, task inputs, model, tools, and request budget as the optimized variant.
_Avoid_: old PaiCLI version, larger-window control, production mode

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
