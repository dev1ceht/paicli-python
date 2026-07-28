# Runtime Session Integration

PaiCLI uses the user-level Session database as the sole durable store for Runtime threads and
Background Tasks. The former `runtime.db` thread/event store and `tasks.db` task store are not
opened, migrated, or used.

## Runtime hierarchy

Each workspace has a reusable Runtime root Session. `POST /v1/threads` creates a
`runtime_thread` child Session below that root. A Background Task creates a
`background_task` child Session below the Session that submitted it; tasks submitted directly
to the Runtime use the Runtime root. Retrying a failed task creates a
`background_task_retry` child below the failed task Session.

The normalized `session_relationships` table supports parent and child queries. Both sides of
the relationship also receive hashed `session.child_linked` and `session.parent_linked`
events, so the relationship is represented in the append-only history rather than existing
only as a mutable projection.

## Runtime threads

Runtime thread IDs are Session IDs. A turn obtains the normal renewable Session lease,
restores provider-valid model history, and persists the User, Assistant, Tool Call, Tool
Result, and Turn boundary through the same interfaces as the TUI. A later HTTP request
therefore continues the earlier conversation instead of starting with an empty Agent.

`GET /v1/threads/{id}/events` serializes the Session event stream as server-sent events.
Concurrent mutation of one Runtime thread is rejected by the Session lease instead of
interleaving two turns.

## Background Tasks

The Background Task queue, checkpoints, and approval projections live in the Session database.
Task API responses retain their existing fields and add `session_id` and
`parent_session_id`. Lifecycle events are appended to the child Session. During execution the
child uses the durable tool sequence from ADR 0040; approval pauses leave the active Turn open,
and approval resume reuses the durable call ID and decision without treating the pause as an
uncertain side effect.

The queue remains an operational projection with workspace-Runtime-root-scoped, renewable
60-second claims and terminal-state guards. Parentage and queue ownership are separate: a TUI
task remains a child of its submitting interactive Session while the workspace Runtime worker
can still claim it. Startup recovery fails only expired claims, so another live Runtime cannot
steal or invalidate work. Task creation, lifecycle transitions, cancellation/Turn interruption,
and approval pause commit their normalized projections and Session facts in one SQLite
transaction. The child Session is the durable Agent record; it does not turn a Background Task
into an interactive Session or make task cancellation a forceful external-operation abort.

## Compatibility and boundary

There is intentionally no compatibility reader or migration for legacy `runtime.db` or
`tasks.db` contents. New Runtime state begins in `~/.paicli/sessions/sessions.db`.

This phase does not model Plan tasks as child Agents. Plan child Sessions and their aggregation
semantics remain the next delivery phase.
