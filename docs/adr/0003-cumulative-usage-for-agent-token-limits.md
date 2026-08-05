# Cumulative usage for agent token limits

Status: superseded.

This ADR documented a former design in which PaiCLI enforced a run-level token limit using cumulative model usage. PaiCLI no longer stops an Agent run based on cumulative input and output tokens.

Current behavior uses the model's per-request output limit (`max_tokens`) together with the context-window manager. Benchmark runs retain independent turn, tool-call, and elapsed-time safeguards. Cumulative usage remains available for telemetry and reporting, but it is not an execution cutoff.
