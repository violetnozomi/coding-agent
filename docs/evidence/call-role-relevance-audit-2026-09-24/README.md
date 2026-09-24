# Call-site usage role audit

Classification: **CALL-ROLE: MIXED**. Local metadata feasibility separately meets
STRUCTURAL-METADATA-SUFFICIENT. Baseline: 4695b46d89cce8366eaaf7941448bc972c476953.
No production, Sidecar, graph schema, frozen workspace or historical evidence was
changed. Zero paid/main/verifier/planner/embedding/InfCodeX requests.

## Pipeline and first information loss

PythonAstAnalyzer.analyze_file calls ast.parse and owns the complete tree.
DirectCallVisitor collects actual Call nodes, excluding nested function/class/lambda
bodies according to its existing traversal. Python nodes have no parent pointer,
but traversal context or a temporary parent map is sufficient. The information is
still recoverable at RawCallRecord creation in this same analyzer. It is lost when
that record retains names, identity, qualifier, line and confidence without role.

PersistentCodeIndex._replace inserts raw calls into calls SQLite rows;
_resolve_calls/_resolve_relations add resolved identities. _row_edge materializes
CallEdge; callers/callees/file_calls and snapshot reuse it. _edge_dict exports the
edge in symbol_context, then RepoIntelligenceService adds query freshness and
cache metadata. None of these current downstream representations has usage_role.
ReferenceEntry's line text cannot recover per-Call nesting reliably.

## Experiment method

The evidence script observes the exact tree parsed by production PythonAstAnalyzer
and instruments RawCallRecord creation to identify the exact Call object. A parent
map is transient evaluator metadata, not a second parser or persisted graph. The
real PersistentCodeIndex/RepoIntelligenceService independently parses, stores and
resolves each disposable workspace. The audit pairs raw occurrences with insertion
ordered DB rows, checking caller identity, raw name, qualifier and line. No proposed
role enters SQLite or CallEdge. Repeated identical names on one line are included
to catch occurrence-level confusion. Future production should attach role at
emission directly, not use this evaluator pairing/instrumentation.

## Observed results

18 generic cases plus C pass under offline_exec. Returned/discarded calls separate;
nested calls on the same line separate returned/argument, including Payload(Payload()).
Assignments stay assigned even when subsequently returned. Yield is yielded but
excluded from the proposed returned-only v1. If-test calls are condition. Arguments
and keyword arguments are argument. Multiple returned classes remain multiple
eligible candidates, requiring independent count/character budgets.

Dynamic factory() retains returned but no resolved class identity. factory.build()
is returned but resolves to a function, so neither is class-eligible. Wrapped bool
expressions and yield-from are conservatively unknown; assignment expressions are
assigned. No variable propagation or general value-flow analysis is performed.

Frozen C uses the exact historical changed-files set on a disposable copy. All
changed Python files are observed and all calls resolved. The name-independent
returned-class filter naturally selects the Config callee of loads; only the final
evaluator assertion names Config. No task prose or hidden acceptance is used.

## Relevance and trigger boundary

Returned-only removes the discarded/argument AuditMarker examples but also selects
return TelemetryMarker(). It cannot certify that a returned class participates in
serialization/persistence. Assigned-then-returned is a deliberate false negative;
wrapped constructors and yielded constructors are additional bounded exclusions.

semantic_pending represents unresolved semantic requirements, not a classifier of
serialization/persistence. changed_scope.risk is structural impact (edges/modules/
public symbols), public_api_exposure reports exports. Persistent-data-deletion risk
is a narrower review signal, not an exhaustive write-boundary detector. Current
Sidecar dependency collection runs from _evidence with changed paths and ready
index, not a semantic serialization classifier. These can gate broad optional
review context, but none independently establishes the proposed semantic trigger.
A future policy must explicitly choose structural returned-class context during
semantic review, or provide a separately justified boundary signal. This audit
neither silently supplies that signal nor recommends heuristics based on names.

## Feasibility and limits

Append usage_role='unknown' to RawCallRecord and CallEdge; persist it on each calls
row; preserve it through resolution, LSP augmentation, snapshot and serialization.
Recommend schema 3→4 rebuild of disposable cache, avoiding old unchanged files
retaining uncomputed role indefinitely. Normal incremental reparse replaces call
rows and advances generation; service generation-keyed caches refresh. Role-only
changes must be included in semantic evidence identity when eventually projected.
Language capability/provenance must remain distinct from target resolution: LSP
can resolve a target but cannot invent the usage role of a lexical parser.

Python AST-native feasibility is proven for tested forms only; JS/TS/Go and lexical
fallback remain unknown until independently implemented/tested. No constructor
collector, envelope or reviewer behavior is proven. Multiline decorators and
complete class projection with unsafe truncation=>omit remain separate blockers.

Next implement only call-edge metadata enrichment with schema/API compatibility
regressions if authorized. Do not combine it with Sidecar selection or CF-C3.
