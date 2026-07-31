# JSONL Sessions and SQLite Task Projections

ADR 0043 supersedes the Session-storage and concurrency decisions in ADRs 0038, 0039, 0040,
and 0041. Durable usage semantics from ADR 0042 remain, but usage is read from JSONL rather
than SQLite.

## Decision

PaiCLI stores each complete Session in one append-only JSONL file below
`~/.paicli/sessions/<workspace-hash>/`. The first line is a Session header. Every later line is
a Session Entry with an `id`, optional `parentId`, timestamp, type, and payload. The parent link
forms the Session branch tree and allows the current branch to be replayed without a mutable
event catalog.

Session discovery, latest-Session selection, parent/child lookup, lifecycle projection,
statistics, replay, and sharing scan these files. Large message and tool content stays inline.
There is no Session SQLite database, catalog, Blob store, WAL, hash chain, migration layer, or
compatibility reader. A malformed trailing line is ignored so an interrupted append does not
hide the preceding valid history.

The supported execution model is one process writing one Session file. Session leases,
renewable ownership, cross-process mutation coordination, and idempotency uniqueness
constraints are intentionally absent.

Background-task lifecycle state, checkpoints, and approval decisions remain operational
projections in `~/.paicli/runtime/tasks.db`. A task's Agent conversation and tool history live
in its child Session JSONL file. Task creation and lifecycle changes therefore span two stores
and do not claim cross-store atomicity. The scheduler is single-process and has no claim token,
claim TTL, or multi-worker ownership protocol. On startup, tasks left `running` are failed and
their active child-Session Turns are interrupted.

## Consequences

Sessions are portable, inspectable, stream-friendly, and structurally close to pi's Session
model. The tradeoff is directory scanning instead of indexed Session queries and no atomic
transaction between task projections and Session history. SQLite remains appropriate for task
state because approval and checkpoint transitions benefit from compact transactional updates;
it is not an index for Session content.

Existing SQLite Session data is intentionally not migrated. New state starts in the JSONL
Session directory and independent task database.
