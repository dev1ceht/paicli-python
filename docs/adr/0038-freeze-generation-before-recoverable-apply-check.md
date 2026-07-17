# Freeze generation before recoverable apply-check

SWE-bench attempt lifecycle is `not_started -> model_running -> generation_frozen -> completed|agent_error`, with `interrupted` represented as an `agent_error` termination reason. Once patch, response, safe events, usage, and their hashes are frozen, resume may rerun only the deterministic clean-base `git apply --check`; it must not resample the model. A stale `model_running` attempt is terminalized with an empty patch and `interrupted` evidence, then later scheduled attempts continue.

One process lock protects each experiment directory. Active owners reject concurrent generation, while dead same-host owners are treated as stale. This preserves one-sample pass@1 semantics while allowing infrastructure work after the model boundary to recover.
