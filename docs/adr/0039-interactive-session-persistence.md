# Interactive Session Persistence

PaiCLI's interactive entrypoint now binds each TUI instance to one durable Session in the
user-level database at `~/.paicli/sessions/sessions.db`. The historical `start_repl` name is
retained, but it launches the Textual TUI; both the entrypoint and the application share the
same `InteractiveSession` coordinator.

## Startup and replay

Startup resumes the most recently updated, non-archived, non-deleted, non-corrupt Session for
the canonical workspace root. When none exists, PaiCLI creates one. The Session's model
projection becomes `Agent.history`, while the complete Session history, including partial
Assistant messages, is rendered on the conversation canvas.

Operational tool-call state is not reconstructed in this phase. Durable tool execution and
recovery remain phase-three work.

## Turn persistence

Accepting a normal TUI submission appends `turn.started` and the user message in one
transaction before invoking the Agent. A successful `done` event appends the accumulated
Assistant text, including an explicit empty Assistant fact for a tool-only completion, and
`turn.completed` in one transaction.

If execution is interrupted, errors, or ends without `done`, PaiCLI atomically appends at most
one `message.assistant.partial` containing the text already displayed and `turn.interrupted`.
Partial messages remain visible after restart but never enter `Agent.history`.

`/clear` only clears rendered conversation widgets. `/reset` appends `context.reset`, clears
the Agent's active model context, and clears the canvas without deleting Session history.

## Session commands

`/session` exposes workspace-scoped lifecycle operations:

- `list` shows current, archived, and deleted Sessions for the current workspace.
- `new [title]` creates and activates an empty Session.
- `resume <id>` activates an existing Session, unarchiving it when necessary.
- `fork [title]` creates and activates a fork at the latest safe semantic boundary:
  `turn.completed`, `turn.interrupted`, or `context.reset`.
- `archive` archives the current Session and activates a new writable Session.
- `delete` moves the current Session to the recycle bin and activates a new writable Session.
- `restore <id>` restores a deleted Session and activates it.

Switching Sessions resets the in-memory context manager, reconstructs `Agent.history` from the
new Session's model projection, and redraws its Session history.
