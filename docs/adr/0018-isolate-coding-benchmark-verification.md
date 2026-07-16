# Separate coding benchmark verification from the Agent workspace

Coding benchmark tasks may expose public tests for development feedback, but final correctness will be determined in a separate verifier workspace using fingerprinted acceptance material preloaded before the Agent starts. This guarantees acceptance integrity even if the Agent changes public tests or reachable files, but it does not guarantee acceptance confidentiality while arbitrary shell execution lacks filesystem isolation; artifacts must record that limitation, and stronger hidden-test claims require an execution sandbox.
