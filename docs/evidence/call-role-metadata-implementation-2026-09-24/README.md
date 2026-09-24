# Production call-edge usage-role metadata

Classification: **CALL-ROLE-METADATA-FIXED**.
Baseline: `64a6bba2983f363b14ebb1a936d3715ef87ff2a5`.

## Scope

Only production `intelligence/analyzers.py` and `intelligence/code_index.py` change.
No Sidecar, dependency collector, task contract, ledger, prompt, completion policy,
repository topology, relevance selector, trigger or source envelope change.
Paid/main/verifier/planner/embedding/InfCodeX calls: zero. Test commands run under
existing offline_exec, which denies IPv4/IPv6 sockets including child processes.
No repository imports, decorators or bodies execute during role extraction.

## RED and implementation

`red/harness-first.txt` preserves the initial test author's missing max_files
argument; those setup failures are not production RED. After correcting the test,
`red/focused-tests.txt` records 38 failures before any production changes. They
exercise absent raw metadata, DB version/column, edges, all query surfaces, cache
migration, LSP preservation and frozen C.

The existing PythonAstAnalyzer parses once. DirectCallVisitor carries a temporary
traversal stack and stores `(concrete_call_node, local_role)` together; the role is
attached at RawCallRecord emission. No name/line pairing, source regex, second parse
or persistent parent graph. Exact immediate uses map to returned, yielded, assigned,
discarded, argument, condition; all others remain unknown. Bool wrappers and
YieldFrom are intentionally unknown. Assignment is not upgraded by a later return.

RawCallRecord and CallEdge append usage_role='unknown'. Calls SQLite adds
usage_role TEXT NOT NULL DEFAULT 'unknown', and schema increases from 3 to 4.
The existing disposable-cache rebuild path clears v3 fingerprints and derived rows;
unchanged files are parsed on the next scan. Normal v4 warm reuse does not reparse.
_replace writes role; _row_edge reads it; _edge_dict and process steps expose it.
Resolution and LSP update only existing target fields and leave role intact.

ProcessStep also appends the same defaulted field so both process edges and steps
carry the originating call role; the process entry uses unknown. No traversal or
selection rule changes. Other language emission sites keep their positional
arguments and obtain unknown by default.

## Verification

`after/focused-first.txt` preserves a harness assertion that incorrectly expected
query payload generation (the supported source is service.state.generation).
After correcting it, `after/focused-tests.txt` has 125 passed. Three extra boundary
tests (nested scope, non-Python registry defaults, late target resolution) bring
`after/focused-final.txt` to **128 passed**, including existing dependency regressions.
The first broad run printed 1784 passed / 11 skipped but received an audit SIGINT
during a prolonged fake-loop wait; it is retained as broad-first-signal.txt and is
not the clean final result. The isolated current fake-loop test also waited >75s;
a clean baseline archive reproduced the wait and exited 124 at a 75s bound. No
unrelated production patch was made. The second full broad run stalled again and was bounded by audit termination
after >220 seconds; see broad-unbounded-wait.txt. The remaining broad set excluding
only test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget is recorded
in after/broad-tests.txt. It is not a claim that the complete unfiltered suite
passed without intervention. The excluded test requires separate baseline-loop
investigation, outside this metadata patch.
The bounded remaining suite ended **1 failed, 1785 passed, 11 skipped, 1 deselected**.
The failure was test_provider_stall_sidecar_cancel_retires_gateway_poll (an empty
provider accounting dictionary at cancellation). Current and clean-baseline isolated
rechecks both passed once. This suggests a suite/timing-dependent failure but does
not prove its exact cause; broad is not reported all-green. No historical log is overwritten.

Tests cover all seven roles, exact nested same-name same-line occurrence ownership,
condition forms, NamedExpr/AnnAssign, unresolved targets, method/function kinds,
fresh v4 and simulated v3 caches, cold rebuild/warm reuse, incremental edit and
service cache invalidation, LSP returned/unknown preservation, positional APIs,
callers/callees/file_calls/snapshot/symbol_context/process_context. Non-Python JS,
TS and Go registry/fallback outputs remain unknown; this is not cross-language role
support. Parser count is asserted once per analyzer invocation.

Frozen C uses historical changed paths on a disposable copy. The production query
naturally yields loads→Config, imported-binding, confidence 0.98, usage_role=returned.
Only the evaluator assertion names Config; production extraction contains no C
symbols or path heuristics. Original workspace and evidence remain byte-identical.

## Boundaries

This closes metadata transport, not semantic proof or relevance. Returned
TelemetryMarker still has returned role. Assigned-then-returned remains assigned.
No constructor source is added to any packet, no class selector is implemented,
and no serialization/persistence trigger is invented. LSP's resolution source can
change while the parser-derived role stays stable; future consumers must not equate
target resolution confidence with a new inferred role.

Next resolve the explicit constructor-evidence relevance/trigger policy. Complete
multiline decorators and safe whole-class envelope omission remain separate work.
Do not run CF-C3 until a separately authorized evidence-channel implementation exists.
