# Guarded finalization after agent limits

When a ReAct safety limit is reached, PaiCLI will permit one final model turn with no tool access before ending the run. This preserves a useful evidence-based answer without allowing the limit itself to restart the tool loop.
