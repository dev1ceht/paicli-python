# Safety controls improvement plan

## Goal

Make PaiCLI's approval, path safety, command policy, snapshot behavior, and audit trail match an approval-first local CLI security model without claiming execution sandboxing.

## Decisions

- Workspace writes, snapshot restoration, and every MCP invocation require approval by default.
- A session allowlist is exact-tool-name only. It never grants another MCP tool or disables policy checks.
- Unattended mode is an explicit, session-only `/hitl never` choice. It may run commands and MCP actions without prompts, but never bypasses policy, path checks, or audit logging.
- Snapshot restoration is a mandatory-confirmation operation even in unattended mode.
- Snapshots omit symbolic links and report the number omitted.
- Command policy runs as a preflight before prompting and again immediately before execution.
- Mandatory audit covers every sensitive operation and every MCP invocation. It records redacted, limited summaries and fails closed if persistence is unavailable.
- OS/container sandboxing is a future optional enhancement, not an assertion made by this plan.

## Implementation sequence

1. **Model policy decisions**
   - Add explicit approval classifications: default approval, mandatory confirmation, session-allowlist eligible, and unattended eligible.
   - Set `write_file` and all dynamically registered MCP tools to default approval. Preserve `revert_turn` as mandatory confirmation.
   - Replace approval-screen `Approve All`/YOLO behavior with `Allow this tool for this session`, backed by an exact-name in-memory allowlist.
   - Keep `/hitl never` as the only route to unattended mode; show a persistent warning in both REPL and TUI.

2. **Preflight and execution guards**
   - Move command-policy evaluation into the executor's pre-approval path, return a precise policy-denial reason, and retain validation in `bash` immediately before spawning.
   - Keep `PathGuard` as the sole resolver for built-in file paths. Audit every path-consuming built-in and snapshot restore path.
   - Update snapshots to detect and skip symlinks during tree creation and restore; expose omitted-link counts in status/results.
   - Document that a command's `cwd` is not an execution sandbox.

3. **Mandatory audit trail**
   - Extend JSONL events with a call ID, decision source (`prompt`, `session_allowlist`, `unattended`, `policy`), denial reason/rule, duration, and redacted result summary.
   - Always audit MCP invocations, including read-only calls; always audit sensitive execution, policy denial, and user denial/skip.
   - Redact secrets recursively and cap result summaries. Store file targets, byte counts, and content hashes instead of file contents.
   - Ensure audit storage is writable before a protected action executes and fail closed if it is not.

4. **Configuration and UI migration**
   - Change default configuration to approval-first behavior without silently enabling unattended mode for existing users.
   - Remove the approval dialog's global mode-switch binding; add a distinct exact-tool session-allowlist action.
   - Make status displays distinguish `auto`, `unattended`, and mandatory-confirmation behavior.
   - Update README, `/hitl` help, and policy documentation with the exact local-execution boundary.

5. **Verification**
   - Add tests for default write/MCP approval, exact allowlist scope, unattended-mode behavior, and mandatory snapshot confirmation.
   - Add policy tests proving denied commands never prompt and are still rejected if execution is invoked directly.
   - Add path/symlink tests for file tools and snapshots, including an external directory-link fixture.
   - Add audit tests for MCP reads, decision sources, redaction, summary limits, daily JSONL rollover, and fail-closed persistence failures.
   - Run the full test suite and perform manual TUI/REPL approval-flow checks.

## Acceptance criteria

- A default session prompts before every write, restore, and MCP invocation.
- Allowing one tool for the session never allows a different tool.
- `/hitl never` suppresses eligible prompts but cannot run `revert_turn` without confirmation or bypass command/path/audit policy.
- Commands rejected by policy never reach the approval prompt or process spawn.
- No snapshot operation follows a symbolic link outside the workspace.
- Every protected action and every MCP invocation has a redacted JSONL record; an audit-write failure prevents execution.
- User-facing documentation never describes path checks, `cwd`, or blacklists as a sandbox.
