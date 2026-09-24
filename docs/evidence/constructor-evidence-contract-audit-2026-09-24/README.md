# Constructor evidence contract audit

Overall: **CONSTRUCTOR-EVIDENCE-CONTRACT: READY-FOR-IMPLEMENTATION** (design contract only).
Baseline: `8702e10aa52638a154f8466c10d478c30a576d38`.
Production diff: **zero**. Paid/main/verifier/planner/embedding/InfCodeX requests: **zero**.
No Sidecar packet, constructor collector, prompt, cache policy or schema was changed.

## Three decisions

| Contract | Classification | Boundary |
|---|---|---|
| Relevance | RELEVANCE-POLICY: BOUNDED-STRUCTURAL-CONTEXT-ACCEPTABLE | Optional structural context, never guaranteed semantic relevance |
| Role provenance | ROLE-PROVENANCE: CALLER-CAPABILITY-SUFFICIENT | Current built-in v4 pipeline plus same-snapshot caller file AND symbol provenance/freshness |
| Source envelope | SOURCE-ENVELOPE: METADATA-ENRICHMENT-REQUIRED | Original AST knows common complete spans; persisted index does not preserve decorator-aware start |

“Ready” means the selection, trust, source-completeness, budget and omission rules are now specified without requiring a new semantic classifier/type/dataflow engine. Additive source metadata and production regression work are still required before deploying a collector. This is not a claim that the current collector already meets the contract.

## Relevance and responsibilities

Repo Intelligence supplies structural facts; evidence policy chooses bounded context; a reviewer makes semantic judgments. The proposed policy uses active semantic review, changed implementation caller, high-confidence resolved unchanged class, trusted AST-native **returned** role, current safe source and strict budgets. No serialization/persistence detector is invented. No analyzer emits constructor_relevant, unsafe or semantic verdicts.

Returned TelemetryMarker remains eligible. We accept this as a bounded structural false positive, consistent with the existing direct-caller dependency channel, which also does not prove task relevance. Worst case both slots are irrelevant. This can distract a reviewer or displace a useful later class; tests bound the cost but do not measure or eliminate that risk. Strict semantic Policy A currently lacks a trustworthy host classifier and would require separately justified capabilities. See relevance/policy-comparison.md.

Six fixtures use real PythonAstAnalyzer → PersistentCodeIndex → RepoIntelligenceService:

| Fixture | Eligible |
|---|---|
| return Payload() | Payload |
| return TelemetryMarker() | TelemetryMarker |
| AuditMarker(); return Payload() | Payload only |
| two branch returns | Payload and TelemetryMarker |
| return Wrapper(Payload()) | Wrapper only |
| obj=Payload(); return obj | none |

Assigned/wrapped objects, unresolved calls, unknown roles, unsupported languages and budget omissions are explicit false-negative boundaries. No variable/type/dataflow inference or name/path semantic ranking is used.

## Role trust and LSP

The LSP fixture uses real augment_call_targets with a deterministic resolver. `edge.source` changes from python-ast to lsp-definition; returned remains returned and unknown remains unknown. Thus edge.source describes target resolution and is insufficient as role provenance.

Caller capability is sufficient only with the full current pipeline invariant: same consistent snapshot, matching caller identity and file, Python/python-ast/AST_NATIVE on file and owner, no parse_error, unchanged current source fingerprint, non-unknown role. `_replace` persists caller file/symbol/calls from one analyzer result; current LSP updates only target metadata. Stale/non-Python/lexical/parse-error/wrong-source cases fail trust checks. An isolated capability string or standalone edge is insufficient.

An explicit future usage_role_source is optional for this closed pipeline, recommended if independent producers, mixed parser origins or standalone consumers break that inference. No new field is implemented. See provenance/field-options.md and provenance/lsp-resolved.json.

## Envelope findings

The offline observer captures ClassDef and decorator coordinates at the **existing analyzer's original SymbolRecord emission**. It returns the original production record unchanged and asserts one parse per native file. It does not independently discover symbols with ast.walk, second-parse files, reconstruct calls or modify the index schema. This instrumentation is an experiment, not a supported production extension point.

Common single-line, multiline call-form, stacked decorators, nested classes, explicit __init__, __post_init__, class validator and inheritance declaration fixtures preserve the complete local class source. Plain class line/end_line misses decorators. Recommend future generic nullable `source_start_line`, computed at original AST emission, transported through derived index metadata and queries with a cache rebuild; keep existing class line/navigation semantics.

One additional counterexample matters: `@(\n decorator\n)` places decorator expression.lineno on the inner expression rather than the @ line. Naively taking min(lineno) is not sufficient for all legal Python decorators. The prototype checks the exact AST-coordinate prefix for indentation + @; unsupported starts are omitted, never repaired by backscan/regex. All decorators must certify their start. Future metadata must retain only certified starts, otherwise unavailable/omit. See envelope/parenthesized-decorator.json.

Complete local source does not prove external decorator, import alias, inherited base or metaclass behavior. The evidence reports code, not “this class permits invalid state.” No repository source, import or decorator executes.

## Truncation, ordering and budgets

Character slicing is unsafe: the long-class fixture's late __post_init__ is absent from its first 1400 chars. Recommend **whole-envelope include-or-omit**, with no method-name selective projection in v1.

Stable order: deduplicated target identities sorted by relative path and symbol_id. No semantic score. Proposed budgets: **2 symbols**, **1400 characters per complete rendered block**, **2400 total rendered section characters**, including labels/headers/separators/omission footer. Reserve footer capacity. Existing bounded-query infrastructure should cap 32 changed roots, 16 candidates and a completion-safe outer wait; prototype full snapshots operate only on bounded disposable fixtures and are not a production query implementation.

| Budget fixture | Included | Rendered chars | Omitted |
|---|---:|---:|---:|
| short | 1 | 411 | 0 |
| medium | 1 | 944 | 0 |
| oversized | 0 | 196 (diagnostic only) | 1 |
| two short | 2 | 627 | 0 |
| two medium | 1 | 1392 | 1 |
| five short | 2 | 627 | 3 |

The candidate may be wholly omitted for size, count, total budget or uncertified span. No partial class can masquerade as absence of validation. Production empty-evidence rendering remains a future integration decision; the offline view retains diagnostics.

## Frozen C

A disposable copy of historical final-files is indexed with the actual historical changed-file set. The same generic selector naturally returns loads → Config, imported-binding, confidence 0.98, usage_role returned. Only fixture assertions name Config; selection never queries task prose, acceptance results, field shapes or known-defect names.

The proposed span is lines 4–9, while current class span is 5–9. It includes `@dataclass(frozen=True)` and the entire class, including enabled default. The rendered block is **324 chars**. Its envelope SHA256 is `3dfd239f23a4df90066f09e66d075c5267edc391cdfb59980c4dfed409d5513b`. It was **not injected into Sidecar**. See envelope/frozen-c-config.json and c/candidates.json.

## Safety and future cache

Reuse WorkspaceFileAccess and current dependency source exclusions. Require workspace match, no symlink/nonregular/escape, bounded UTF-8 current source, no hidden/tests/docs/evidence/oracle/acceptance/generated/vendor/authority paths. Current caller and target fingerprints and generation must agree before/after collection; unavailable/building/stale/timeout/exception => optional omit. Network/model imports and socket transports are denied by the audit script; the outer existing seccomp harness also denies IPv4/IPv6. No embedding or semantic search is invoked.

Future evidence digest should bind path, symbol identity, role plus caller-derived provenance, target confidence, full current source hash, complete rendered envelope hash, source span, inclusion/omission/completeness state and policy version. Exclude query duration/cache-hit and housekeeping generation if semantic evidence is unchanged. None→available or source change must miss; identical semantic evidence should remain stable. No semantic cache is modified here.

## Verification and integrity

- audit.py: 6 real-graph relevance fixtures, 10 envelope fixtures, 6 budget cases, LSP trust/unknown preservation, capability/stale negatives, frozen C and safe-source checks passed.
- Existing related regression: **118 passed in 3.82s** (call role, code index, service, workspace access, dependency evidence).
- Ruff/compile/diff checks and hash verification are recorded separately.
- Early harness errors are preserved in run-first through run-fourth; they are not production RED. See analysis/harness-errors.json. run-fifth and run-final passed; final reproducibility log records formatted script rerun.
- Baseline source-hashes.json freezes all **3578** tracked production/history files and matches their baseline Git bytes. verify.py checks those and this directory's SHA256 manifest. Original frozen files and prior evidence are unchanged.

Broad suite is **not rerun or claimed green**. Prior evidence retains a clean-baseline fake-loop long wait, isolated baseline/current stall-accounting passes, and incomplete full-suite causal attribution. Neither test_provider_stall_sidecar_cancel_retires_gateway_poll nor test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget is fixed or attributed to the role patch here.

## What follows

This establishes a bounded optional-context policy, a current-pipeline role trust contract and a safe include-or-omit source-envelope design. It does not establish semantic relevance for every class, proof of runtime construction behavior, reviewer reliability, future C verdict, a unique false-accept cause or Main Agent improvement.

Next: implement/test certified source-start metadata, then a small bounded collector and Sidecar supporting-evidence/cache/trace integration in a separately scoped phase. Preserve all current authority/closure/prompt semantics. No CF-C3 or paid request is authorized by this audit.
