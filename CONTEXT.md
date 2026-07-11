# PaiCLI

PaiCLI is a terminal AI agent that accepts user input in a Textual TUI and relays it to an OpenAI-compatible model service.

## Language

**TUI submission**:
A non-empty message or slash command accepted by the focused PaiCLI input field after Enter is pressed.
_Avoid_: typing, draft

**Startup banner**:
The compact, pre-conversation information area displayed when the PaiCLI TUI opens; it presents application identity and current session capabilities.
_Avoid_: splash screen, chat area

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

**Context-management evaluation**:
A paired experiment that compares PaiCLI context-reduction variants for task quality and context consumption.
_Avoid_: context test, compression test

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
