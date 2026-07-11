# Approval-first local execution boundary

PaiCLI will use approval, real-path validation, destructive-command policy, mandatory JSONL auditing, and snapshot recovery as its local execution controls. It will not claim that these controls are an execution sandbox or require container/VM isolation in this iteration. Unattended mode remains an explicit session choice, while snapshot restoration always requires approval and all safety policies remain non-bypassable.

## Considered Options

- Require an OS/container sandbox before command execution: rejected for this iteration because it adds substantial platform, environment, and dependency cost to a local CLI.
- Treat a workspace working directory and command blacklist as a sandbox: rejected because neither creates an OS-enforced boundary.

## Consequences

Command execution must be described honestly as user-authorized local execution subject to policy, not as workspace isolation. A future OS-level sandbox can be introduced behind a command-runner boundary when unattended command execution needs stronger containment.
