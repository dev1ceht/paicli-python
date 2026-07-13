# Cumulative usage for agent token limits

PaiCLI will enforce a run-level token limit using the cumulative actual input and output tokens reported by all model calls in that run. When a provider omits usage, PaiCLI will conservatively estimate the input and reserve the configured maximum output. Context-window capacity remains a per-request constraint and is not a substitute for a cost-control limit.
