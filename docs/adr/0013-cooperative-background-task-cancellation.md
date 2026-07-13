# Cooperative background-task cancellation

PaiCLI will cancel background tasks through per-task in-memory signals while retaining SQLite as the durable task-state authority. The signal is checked at shared Agent and tool execution boundaries, and cancellation propagates as control flow rather than a tool error; the first phase deliberately does not abort in-flight HTTP requests, tools, or processes, so ordinary interactive Agent execution remains unchanged and long-running integrations can gain resource-specific interruption later.
