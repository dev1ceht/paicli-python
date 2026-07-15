# Isolate coding benchmark verification from the Agent workspace

Coding benchmark tasks may expose public tests for development feedback, but final correctness will be determined by an acceptance verifier whose validation material is unavailable and immutable to the Agent during the attempt. The verifier will evaluate the Agent's resulting change in a separate workspace derived from the original fixture, accepting extra workspace and test-provisioning complexity to prevent altered or deleted tests from producing false passes.
