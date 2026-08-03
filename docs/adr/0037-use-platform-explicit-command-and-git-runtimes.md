# Use platform-explicit command and Git runtimes

PaiCLI exposes one production `bash` tool. It invokes a non-interactive Bash executable on every platform, streams stdout/stderr as tool progress events, persists those events for Runtime SSE replay, supports an optional per-call timeout, and terminates the complete process tree on cancellation or timeout. The workspace is the command working directory, and terminal input is disabled.
