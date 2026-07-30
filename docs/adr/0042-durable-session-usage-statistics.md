# Durable Session Usage Statistics

PaiCLI records model usage as append-only `usage.recorded` Session events. The event payload
contains a stable usage and request identity, provider and model, purpose, actual-or-estimated
source, uncached input, output, cache-read and cache-write token counts, and an optional
currency-qualified cost with its own source.

## Live and durable views

The TUI uses two stable status lines. The first shows shortened workspace, Git branch, Session
title, execution phase, and elapsed time. The second combines an in-memory snapshot of durable
Session token, cache, and cost totals with the live request-scoped context window, compression
pressure, provider, and model. It does not repeat the latest request's input, output, or cached
token reading. Narrow terminals truncate both sides while preserving some cumulative usage and
live context information.

Persisting a usage event refreshes the durable snapshot off the UI thread; ordinary status
refreshes never scan SQLite. `/session stats` performs an explicit complete aggregation off the
UI thread and shows token, cache, cost, activity, inherited-history, and coverage details.

Provider token counts are facts. When a provider reports prompt tokens that include cache
reads, PaiCLI stores uncached input separately by subtracting cache reads. The existing local
price catalog produces a `catalog_estimate` cost and the UI prefixes it with `≈`; it is not
presented as a provider-billed amount.

## Replay, compatibility, and forks

Session statistics are derived from events rather than stored as mutable counters, so close,
resume, verification, and replay produce the same result. Usage writes use the active Turn and
the stable usage identity as their idempotency key. Multiple provider usage chunks for one
request are coalesced into one final usage fact.

Older turns with Assistant output but no corresponding usage event remain readable and report
`partial` coverage instead of inventing zero-cost certainty.

Forked usage events retain source provenance. Their tokens are reported as inherited history
and are excluded from the child branch's own token and cost totals. New usage produced by the
child is counted normally.
