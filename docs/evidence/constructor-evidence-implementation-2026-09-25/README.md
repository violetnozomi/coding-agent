# Bounded constructor supporting evidence

Classification: **CONSTRUCTOR-EVIDENCE-IMPLEMENTED**.
Baseline: `8bc90584eedbb696b497e885a8989ac1885c357b` (HEAD and origin/main matched, worktree clean).
Paid/main/semantic-verifier/planner/embedding/InfCodeX requests: **0**. No CF-C3.

## Ordered implementation and real RED

Phase 1 was completed before collector or Sidecar edits. `tests/metadata-red.txt` records **11 failed** for missing source metadata/schema. After adding metadata, `tests/metadata-green.txt` records **66 passed** (new metadata, call-role, code-index).

Phase 2/3 production-facing RED was then written. `tests/collector-composition-red.txt` records **35 failed**, covering missing collector and absent generic/frozen-C Sidecar envelope. The failed frozen-C test saved the actual baseline packet before asserting the missing class. With collector implemented and Sidecar still unchanged, **32 passed / 3 deselected** in collector-green-first.txt (the name filter also deselected the independent unavailable test).

Phase 3 only adds supporting evidence composition/digest and trace. First combined run: **164 passed**. Expanded suite exposed two exact trace-list assertions after the new additive constructor event: **2 failed / 272 passed**, retained in tests/focused.txt. Update those assertions without removing existing events/verdict assertions. Final expanded focused run: **274 passed**. Two further source-race/nonregular/instruction-like-comment regressions bring the final constructor file to **46 passed**. No historical log was overwritten.

## Production changes

1. `nz_coder/intelligence/analyzers.py`: append nullable source_start_line to SymbolRecord; derive certified starts in the original Python AST traversal for ClassDef/FunctionDef/AsyncFunctionDef.
2. `nz_coder/intelligence/code_index.py`: append same default to SymbolEntry; schema **4 → 5**, nullable symbols.source_start_line column; persist, restore and query-expose it. Existing disposable-cache rebuild removes old fingerprints and reparses unchanged source. Tests verify cold rebuild, v5 warm reuse and incremental start change.
3. `nz_coder/runtime/verification/constructor_evidence.py`: new optional bounded returned-class source collector.
4. `nz_coder/runtime/verification/sidecar_verifier.py`: compose existing dependency text plus separate constructor section, combine digests, emit metadata-only trace. Existing accepted cache already binds supporting_repository_digest.

No dependency_evidence.py selection change, new graph, second parser, regex/backscan, type/dataflow engine, semantic classifier, task-prose or fixture-name heuristic. TaskContract/Ledger/completion/terminal/verdict/prompt criteria remain unchanged.

## Certified definition boundaries

Keep `line/end_line` unchanged for navigation. Undecorated definitions start at node.lineno. At each decorator expression's original AST byte coordinate, certify that the preceding prefix is only indentation plus `@`; take the earliest certified start. Single-line, stacked, multiline call-form decorators and nested classes work. Every decorator must certify. Parenthesized `@(\n decorator\n)` cannot certify and returns **None**; the collector omits it instead of reconstructing syntax. Other analyzers default to None.

Only complete start-through-end envelopes are rendered. This retains inheritance syntax, explicit __init__, __post_init__ and all class-body methods/validators. It is local source, not proof of runtime semantics of external decorators/import aliases/bases. No repository import/decorator/body executes.

## Selection, trust and boundedness

Resolved target must be a class at confidence >=0.85, unchanged safe implementation source. Call edge must be directly `returned`; caller file and owner must be same-snapshot Python/python-ast/AST_NATIVE with no parse error and fresh current fingerprint. LSP may change target-resolution source; it does not invalidate the separately checked caller role provenance. Unknown/assigned/yielded/argument are unsupported.

Use production indexed identity relations only: changed-path snapshot → returned target IDs → exact index symbol_context(enrich=False) → scoped snapshot of caller/target files. No full repository source/map projection. Deduplicate target identity, sort relative path then symbol_id. Queries run through existing service bounded-query executor; no index warming or embedding.

Limits: 32 input changed paths, 256 call edges in changed-source snapshot, 16 resolved returned target candidates, **2 included classes**, **1400 chars per complete rendered block**, **2400 total chars** including section/headers/metadata/separators/footer, 128000-byte safe source limit, 0.5s outer wait. The edge/candidate limits may conservatively omit even before class-kind filtering. Timeout bounds the calling thread's wait; an already-running query can finish in the existing worker, as with dependency evidence. No claim of hard CPU preemption.

Read safe current bytes through WorkspaceFileAccess. Match caller/target fingerprints and consistent generation before/after. Any unsupported/unsafe/unavailable path, stale source, race, service unavailable/building/failed, timeout or exception yields optional omission. Over-budget classes are omitted whole, never character-truncated. Late __post_init__ is therefore visible in full or no class source is emitted.

## Generic behavior

| Source form | Included class context |
|---|---|
| return Payload() | Payload |
| return TelemetryMarker() | TelemetryMarker (accepted bounded structural false positive) |
| AuditMarker(); return Payload() | Payload only |
| return Wrapper(Payload()) | Wrapper only |
| obj=Payload(); return obj | none |
| yield Payload() | none |
| unresolved() / non-Python unknown role | none |
| oversized class / uncertified parenthesized decorator | whole candidate omitted |

The host never states “relevant”, “allows invalid state”, or “has no validation”. Wrapped/assigned/dynamic/oversized cases and irrelevant early candidates displacing later useful ones are known precision/recall boundaries. No broader policy was introduced to improve C.

## Authority, security and cache

Section: `=== RELATED UNCHANGED CONSTRUCTOR SOURCE ===`. Label explicitly denies task authority, execution proof, semantic conclusion, instructions and permission. Source comments remain text; trace records hashes/spans/reasons, not source bodies. Tests cover source comments, symlink escape, nonregular target, deletion, oversized/binary source, workspace mismatch, authority path, tests/docs/evidence/oracle/acceptance/vendor exclusion, stale bytes, read race and generation race.

Constructor digest includes policy version, path/identity, returned role, caller role provenance, target confidence/relation, current full source hash, envelope hash, source_start_line/end_line, inclusion/omission reason, rendered chars and text. Even a safely read oversized omitted candidate has a source-sensitive digest. Combine constructor/dependency digests when constructor decisions exist; preserve prior dependency digest otherwise. Stable semantic evidence stays stable through index housekeeping. None→available/source refresh/omitted-source change miss; no semantic-review cache policy change.

## Frozen C offline packet

Using only historical changed paths and a disposable final-files copy, production naturally selects loads → Config (returned/imported-binding/0.98). Metadata gives source_start_line=4, existing line=5, end_line=9. Packet includes `@dataclass(frozen=True)` plus complete Config body including enabled default.

`c/before-packet.json` is saved during real composition RED. `c/after-packet.json` uses current production hook._evidence, not a hand-built prompt and not hook.__call__. Machine diff: **only supporting_repository_evidence and supporting_repository_digest change**. Original dependency evidence remains an identical prefix. New constructor section is **552 chars**. Original complete CONFIG_SPEC, writer diff, store.save/dumps/write_text and 46-pass verification fact remain present. Hidden failed-check name, evaluator acceptance ratio, oracle/acceptance paths and offline counterexamples are absent.

P2 source premise moves from absent to inferable from current complete class source. This does not automatically certify P5 or a full semantic defect proof; no reviewer was called.

## Tests and broad boundary

- Metadata RED 11 failed → phase GREEN 66 passed.
- Collector/composition RED 35 failed → collector-only 32 passed.
- Expanded focused final: **274 passed in 5.28s**.
- Final constructor security/race file: **46 passed in 1.14s**.
- Broad Runtime/Agent Core: **1841 passed, 11 skipped, 1 deselected in 177.54s**. See tests/broad-first.txt.
- Known long-wait `test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget` was explicitly deselected, consistent with prior unresolved baseline evidence. Stall-sidecar remained in broad and passed this run. Neither unrelated issue was patched, and prior suite/timing causal uncertainty remains.
- While broad was still running, progress suggested registry test delay; a separate registry isolated test passed (1 passed). Broad subsequently completed within its 180s bound. No failure or timeout is invented from that observation.
- Ruff, compile and production/test diff checks passed. Raw pytest logs and unified implementation.patch retain original trailing whitespace; whole-evidence whitespace warnings are documented in static-checks.txt. Historical/frozen evidence hashes remain unchanged. Tests use existing seccomp offline_exec; new fixture autoguards forbid actual verifier/semantic configuration.

## Conclusion and next action

This establishes the production path: existing structural identities/returned role → certified current complete class envelope → bounded non-authoritative Sidecar supporting context → cache-bound identity. It does not establish class semantic relevance, reviewer defect detection, a unique historical false-accept cause, universal constructor semantics or improved reliability.

A separately authorized frozen CF-C3 is now technically worth considering, to measure one reviewer response with a clean controlled packet delta. It is **not performed or authorized by this phase**. Stop at host evidence delivery.
