# Evaluate coding tasks through the production Agent path

PaiCLI coding benchmarks will invoke the production Agent orchestration rather than a benchmark-only loop; a benchmark may supply controlled configuration and a model client, but it must retain the normal tool execution, safety, context-management, and lifecycle boundaries. This makes benchmark outcomes representative of the shipped product, accepting additional setup cost and the possibility that production safeguards affect benchmark scores.
