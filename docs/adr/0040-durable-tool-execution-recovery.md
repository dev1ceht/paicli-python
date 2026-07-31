# Durable Tool Execution Recovery

> Partially superseded by ADR 0043: recovery facts now live in Session JSONL and Session leases
> are removed. The unknown-outcome safety behavior remains.

PaiCLI persists each interactive tool-calling step in the same user-level Session database
before allowing the tool to execute. This closes the restart gap left by ADR 0039 while keeping
Runtime background tasks, Plan child Agents, and Runtime API replacement outside this phase.

## Durable tool sequence

When a model turn returns tool calls, PaiCLI atomically appends the Assistant message, including
its original tool-call IDs, arguments, provider call shape, and reasoning-continuity payload,
then creates one normalized Pending Tool Action per call. Only after that transaction commits
does the Agent emit the executable tool-call event. Each action stores its model-turn batch
ordinal so restart preserves the provider's original call order rather than sorting by call ID.

Immediately before dispatch, the Pending Tool Action becomes `executing`. Approval requests and
decisions append `approval.requested` and `approval.resolved` events while projecting their
current state onto the same Pending Tool Action row. A Tool result atomically appends the paired
`message.tool`, including its original `tool_call_id`, and marks the action `completed`.

The append-only Session events remain replayable Session facts. They do not replace the
mandatory redacted security audit trail: the audit log answers who approved and what sensitive
operation occurred, while Session persistence retains the local model/tool sequence required
to continue a valid provider conversation.

## Restart policy

An active Turn that stopped before producing any durable Tool Action has no executable state to
resume. On startup PaiCLI appends `turn.interrupted` with the
`process_restarted_before_tool_state` reason before accepting new input, so the persisted User
message remains visible without leaving the Session permanently active.

An active Turn with durable tool state is recovered automatically:

- `prepared` means execution had not started and may resume.
- `waiting_approval` re-presents approval using the locally stored call, never a model or UI
  payload.
- A durable `deny` or `skip` decision is converted to its deterministic paired Tool error and
  is never re-presented or executed after restart.
- `executing` may resume only when the tool is both read-only and idempotent.
- An `executing` side-effecting or non-idempotent action receives a paired Tool error with an
  `unknown` execution outcome. PaiCLI never repeats it automatically; the model must inspect
  workspace or external state before proposing another action.
- If a Tool result was already durable, PaiCLI continues with the next model request without
  re-executing the tool.

Interrupting a Turn atomically settles every unresolved action with an unknown Tool result
before appending `turn.interrupted`, preserving provider-valid tool-call/result pairing.

## Concurrent interactive processes

Each open interactive Session owns a renewable 60-second Session write lease, refreshed every
20 seconds. Semantic writes must present its opaque token. Normal startup skips a busy Session
and creates or resumes another workspace Session; explicit resume of a busy Session fails
instead of silently sharing it. Expired leases can be taken over, and clean TUI shutdown
releases ownership immediately.

Leases are mutable operational projections rather than audit events, so heartbeats do not
inflate the append-only Session history. Pending Tool Actions are also normalized for efficient
recovery queries, while every semantic transition remains represented by Session events.

## Delivery boundary

This phase does not migrate or replace `runtime.db`, change the Runtime API, model background
tasks as child Sessions, or introduce Plan child Agents. Those remain later delivery phases.
