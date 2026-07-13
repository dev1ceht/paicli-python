# JSON Schema for runtime tool validation

PaiCLI will use `jsonschema` to validate every tool payload before approval or execution, including MCP-provided schemas. This makes the schema already exposed to the model an enforceable contract for types, required fields, ranges, and enumerated values. Rejected invalid calls still consume the run's call budget and participate in repeated-call stagnation detection.
