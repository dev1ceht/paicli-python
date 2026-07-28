# SQLite Session Event Store

PaiCLI will persist interactive sessions in a user-level SQLite database at
`~/.paicli/sessions/sessions.db`. TUI, REPL, and Runtime API integration will share this
repository in later delivery phases. Project directories do not own session databases; each
session instead stores one immutable canonical workspace root.

## Decision

`SessionRepository` is the public persistence seam. A session has two durable views:

- `session_events` is the append-only source of replayable facts.
- `sessions` is the current catalog and lifecycle projection used for efficient queries.

Every event has a session-local contiguous sequence, schema version, previous-event hash, and
event hash. The hash also covers its ordered Blob references. Rebuild rejects sequence gaps,
unsupported event versions, broken hashes, missing Blob references, and corrupt Blob content.
A failed integrity check marks the session `corrupt`; corrupt sessions are read-only and may
only be inspected or permanently purged.

SQLite connections enable WAL, full synchronous writes, foreign keys, a five-second busy
timeout, and secure deletion. Semantic transitions use short `BEGIN IMMEDIATE` transactions.
An optional idempotency key is unique within one session: repeating the same event returns the
original event, while reusing the key with different content is rejected.

Database changes use ordered, transactional migrations recorded in `schema_migrations` and
`PRAGMA user_version`. Upgrades back up the database before applying migrations. Stored events
remain immutable; versioned upcasters adapt older payloads only in memory during replay.

## Replay

The repository rebuilds a `SessionView` containing:

- the complete user-visible session history;
- the smaller message projection eligible for the next model request;
- effective session metadata;
- the most recent context-reset boundary.

`message.assistant.partial` remains visible in the session history but is not replayed to the
model.
`context.reset` clears only the model projection. `message.hidden` changes the effective
projection without deleting the original event.

## Blob storage

Text content larger than 64 KiB and binary content are stored in a content-addressed `blobs`
table using the SHA-256 hash of the uncompressed bytes. Compression is transparent and used
only when it reduces storage. `event_blob_refs` preserves reference order and duplicates in a
normalized association, while the same ordered references are covered by the event hash.
Orphan collection is separate from session deletion so a fork can retain shared content.

## Lifecycle

- Archive appends `session.archived`, hides the session from default lists, and makes it
  read-only until `session.unarchived`.
- Delete appends `session.deleted` and moves the session into a 30-day recycle bin.
- Restore appends `session.restored`.
- Permanent purge deletes the session and its events; unreferenced Blobs are collected
  separately.
- Fork creates a new independent session, copies semantic events with new event IDs and source
  provenance, and reuses Blob hashes. Operational approval, lease, pending-action, and Turn
  events are not copied.

## Delivery boundary

This ADR covers phase one only: repository, schema, event replay, integrity, Blob storage,
catalog metadata, lifecycle operations, and fork. Agent integration, asynchronous Turns,
leases, approvals, tool execution recovery, Runtime API replacement, background-task child
sessions, and Plan child Agents remain explicit later phases.

Legacy `runtime.db` data will not be migrated. Its removal belongs to the Runtime integration
phase rather than this storage-only change.
