# Identify benchmark replicates and controlled comparisons

PaiCLI benchmark runs will record separate identities for the suite content, the exercised PaiCLI runtime, the secret-free benchmark configuration, and the host environment. Attempts may be aggregated as replicates only when all four identities match; a controlled comparison may instead vary one declared dimension, such as runtime or model, while holding every non-target dimension fixed. Development runs may use a dirty runtime when that state and its source fingerprint are visible, while formal runs may require a clean checkout, preserving fast evaluation of work in progress without hiding uncontrolled differences across code, configuration, or operating systems.

For the SWE-bench context comparison, the only permitted target dimension is `context_identity.variant`; dataset and subset fingerprints, PaiCLI runtime, model settings, Agent budget, context-budget values, tool profile, prompt inputs, and host environment must match exactly or the comparison report is invalid.

Development SWE-bench runs may retain a visible dirty source fingerprint, but any formal comparison intended to support external or resume claims must reject a dirty PaiCLI worktree before model execution and run both variants from the same clean commit.
