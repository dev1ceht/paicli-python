# Use a five-task formal subset and a separate Flask pilot

The first claim-eligible experiment uses `context-stress-5-v1`, the first five ordered instances of the previously frozen `context-stress-10` manifest. Each task and context variant runs exactly once, producing 10 serial attempts. Results must be labelled as a fixed five-task SWE-bench Lite context-pressure subset under the named 32K profile, never as a full Lite score.

`flask-pilot-1-v1` contains only `pallets__flask-5063` and is excluded from the formal five. It is the real-model development pilot used before creating a new clean formal experiment. Pilot results cannot be substituted into, aggregated with, or used to replace formal tasks.
