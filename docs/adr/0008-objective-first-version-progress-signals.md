# Objective first-version progress signals

PaiCLI's first progress detector will use only observable signals: empty or unchanged results, repeated read calls, repeated failed commands, and writes without the expected workspace change. Semantic judgments about whether a result adds knowledge are deferred so each stop can be reproduced and explained.
