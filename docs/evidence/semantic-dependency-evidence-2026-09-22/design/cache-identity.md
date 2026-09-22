# Semantic cache identity

The accepted cache key now includes a SHA-256 digest of selected paths, symbols,
relation, current full-source hash, snippet hash, spans, freshness, truncation and
rendered bounded evidence (including omission count). Empty evidence has a stable
empty identity. Nonempty evidence becoming available invalidates an old acceptance.

Identical selection/current bytes is stable across repeated queries or an index-only
rebuild. Query time, cache_hit and housekeeping generation are not hashed. Generation
is checked for consistent scope/snapshot provenance and included in the trace.
Externally edited dependency bytes with stale indexed fingerprints are omitted;
after normal structural refresh the new source hash creates a new identity.
The cache does not infer acceptance from the source or from this digest.
