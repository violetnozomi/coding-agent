# Future schema/API changes (not implemented)

Append usage_role: str = 'unknown' to RawCallRecord and CallEdge, preserving old
positional constructors. Add calls.usage_role TEXT NOT NULL DEFAULT 'unknown'.
Prefer SCHEMA_VERSION 3→4 to use the existing disposable-cache rebuild path; a
column-only migration would leave fingerprint-reused old rows without roles.

Wire _replace, _row_edge and _edge_dict. Existing _resolve_relations and LSP target
updates must preserve role; resolution confidence/source must not manufacture role
trust. Consider separate usage-role provenance/capability if needed: current LSP
augmentation overwrites edge.source. Keep unknown for unimplemented language tiers.

Incremental file replacement recomputes role and advances index generation. Existing
service generation/query caches then invalidate; no new index/graph is needed.
Test cold rebuild, warm reuse, role-only source edit, callers/callees/snapshot,
serialization, unresolved→LSP-resolved preservation, and old API defaults. Consumers
using exact dictionary equality need additive-field regression updates.
