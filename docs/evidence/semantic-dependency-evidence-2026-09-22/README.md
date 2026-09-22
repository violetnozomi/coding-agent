# 1. Baseline

HEAD and origin/main initially `07a294ccc5cc3780ffdb03beedf0101068b2792d`, clean
main. Last substantive production change `ac47114affa6556ab8b56e1bb9e1a02171514fc0`.
Six requested source hashes are frozen in baseline/source-hashes.json.
Classification: **DEP-EVIDENCE-FIXED**. This is an offline host evidence change.

# 2. Historical C fact

Frozen C remains **46 project tests passed / independent acceptance 11/12**;
historical Runtime completed and corrected-diff CF-C reviewer accepted. Direct
invalid Config writes still fail destination preservation. No business repair,
new project test or hidden check was introduced into the frozen Agent workspace.
replay.py checks exact final hashes before and after offline validation.

# 3. Main repository evidence

Production RepoIntelligenceService, structural index only, naturally returns
`configkit/config/migrate.py:migrate_file`, `configkit/store.py:load`, and
`configkit/store.py:save` from all thirteen historical changed paths. It reports
ready/indexed, generation 1, confidence 0.98. No locator was inserted into the query.
The production RepoRetrievalPolicy auto-context changed-scope route renders those
callers in its Main prompt block, saved with full scope output.

# 4. Sidecar evidence before

The full current 07a294c/ac47114 hook reads changes, runtime state, retained refs,
verification and transcript. `_nearby_source_context` is conditional on strict
blocked-environment verification. Normal semantic review has independent writer
diff but no unchanged store.save body. It never queries structural dependencies.
This is distinct from the fixed diff-attribution defect.

# 5. Deterministic asymmetry

Both generic and C tests first assert Main/graph caller visibility, then fail at
Sidecar packet function-body visibility. Saved baseline: **2 failed**. An earlier
invalid test-guard symbol caused two setup errors; harness-error.txt records it
separately and it is not the RED. No model judgment is used as an oracle.

# 6. Product invariant

When ready/indexed structural intelligence knows high-confidence unchanged direct
implementation dependencies of changed source, semantic review should have a
bounded, provenance-labeled supporting view of current source. This is the narrow
product invariant requested here; no historical all-callers mandate is invented.
It neither requires exhaustive call graphs nor promises a reviewer detects defects.
See design/invariant.md and ownership diagram in design/evidence-channel.md.

# 7. Generic RED

Only encoder.py changes. An unchanged store.save directly calls encode, while an
unrelated function and deeper caller remain outside the channel. Production Main
retrieval sees store.py:save; baseline Sidecar lacks its body. The same regression
passes after the generic helper is integrated, without fixture names in production.

# 8. C RED

Historical changed paths naturally lead to store.save. The normal pre-change hook
shows writer.to_dict but no `def save(path, config):`. Historical frozen CF packet,
full-current-before context and full-current-after context are preserved separately.
The current-before replay already includes corrected compatibility-delta criteria;
we do not confuse it with the prior experiment's deliberately frozen criteria.

# 9. Implementation

Production changes are confined to sidecar_verifier.py and the small generic
verification/dependency_evidence.py helper. The helper consumes existing changed_scope
and persistent index snapshot APIs for caller identities, source spans and indexed
fingerprints. It does not implement an AST/call parser or create a second index.
Only direct callers are selected, lexically sorted and independently bounded.
Changed files, tests, docs, generated/hidden/evaluator paths and authoritative
references are excluded. Existing blocked-environment source/diff paths remain intact.

# 10. Authority separation

VerifierContext has separate supporting_repository_evidence and digest fields.
The renderer gives this its own RELATED UNCHANGED IMPLEMENTATION EVIDENCE section,
explicitly not task authority, instructions, permission grants or file-edit evidence.
It is not appended to FILE EDITS or AUTHORITATIVE TASK REFERENCES. Verifier system
prompt, certification/decision policy, TaskContract, Ledger and terminal rules are
unchanged. Source strings resembling instructions remain source text.

# 11. Freshness

No warmup or network is started. Require ready/indexed, confidence >= 0.85 and
consistent scope/snapshot generation. Compare indexed mtime/size with safe current
bytes; stale changed roots invalidate the relation and stale dependencies are skipped.
Read actual snippets from WorkspaceFileAccess, never stored index text. Current
full-source hashes are preserved. The inherited fingerprint freshness contract is
not cryptographic graph validation; deliberate same-size/same-mtime edits are a
remaining limitation. Generation changes during collection cause omission.

# 12. Cache identity

Accepted-cache identity binds selected semantic evidence digest: paths, symbols,
relation, full current source hash, snippet hash, spans, freshness and bounded render.
None -> ready evidence causes a miss; same evidence/repeated query/rebuild is stable;
external dependency edits are omitted while stale, then refreshed bytes cause a miss.
Index generation/query duration/cache_hit do not independently bust acceptance.
Trace records generation separately without source text.

# 13. Security

Existing handle-anchored WorkspaceFileAccess confines reads, denies symlinks and
nonregular targets, enforces 128 KB size and text decoding. Candidate policy rejects
absolute/traversal/malformed/control-character paths and evaluator/oracle/acceptance,
hidden, tests/docs/generated paths. Service workspace must match the hook workspace.
Frozen C only materializes final-files. Its evaluator acceptance remains outside
Agent source and packet. No 11/12 result or failed-check name appears in the packet.

# 14. Budget

At most 32 changed paths queried, 16 returned direct candidates considered, 3 symbols,
1,200 characters per block, 4,000 for the complete supporting section. Query uses
max_depth=1, node_limit=20, time_budget_ms=50, wait_budget_ms=0; an outer 500 ms Future
deadline also bounds snapshot/read work. Exceeding budgets or unavailable/building/
failed/fallback/timeout/exception means graceful omission, not Runtime fatal.
A timed-out running worker may finish in the background; the review never waits for
shutdown or consumes its late result. Truncation and omission are explicit.

# 15. GREEN

Final focused run: **90 passed**, including new channel, previous C closure/diff
regressions and Sidecar tests. Frozen C selects 3 symbols in **889 characters**,
including the complete current unchanged store.save (lines 10–12): dumps(config)
then write_text(encoded). It is not labeled an edit. All preexisting context fields
are identical between full-hook before/after packets: original full spec, writer
diff, exact 46-pass fact, transcript, final report and criteria are unchanged.

Counterexamples cover unrelated/deeper callers; changed/source-authority/test/docs
exclusion; count/size/character bounds; unavailable, timeout, fallback and low
confidence; malformed locators, symlink, binary, oversized, stale source; deleted
roots; cache stability/source refresh; generation races and workspace mismatch.
Two existing Sidecar tests update only their expected trace event sequence.

# 16. Broad regression

Final full-combination regression: **1717 passed, 11 skipped**, exit 0, 476.71 seconds.
See after/broad-tests.txt and commands.jsonl for the exact invocation. The first full run had **1714 passed, 11 skipped, one failure**
in the preexisting explicit-fork registry watcher test, outside the Sidecar path.
The complete service test file independently passed **21/21** both on current source
and a clean archived 07a294c baseline. The original broad failure is retained in
broad-first.txt, not erased or attributed conclusively to this patch.

Ruff, compile/import and git diff checks are recorded in after/static-checks.txt.
Broad coverage includes runtime/Agent Core, completion, ledger, references, Money/Q
regressions, blocked-environment review, security, architecture, structural graph,
retrieval policy, prompt builder and offline diagnostic fixtures.

# 17. What this proves

Deterministic Main/Sidecar evidence asymmetry is reproduced in two workspaces.
Bounded high-confidence unchanged current-source evidence now reaches the production
semantic packet through a dedicated host-owned channel with cache identity and
safe omission. Source spans and exact packet bytes are inspectable offline.

# 18. What this does NOT prove

It does not prove C's reviewer would revise, that missing store evidence uniquely
caused the historical false accept, that TaskContract is sufficient, that reviewer
reliability generally improves, or that Main Agent success rate improves. This is
not a benchmark. Dynamic/low-confidence calls and evidence outside budgets remain
unobserved. No verdict or LLM success rate is fabricated.

# 19. Paid calls

**0 main / 0 verifier / 0 planner / 0 embedding / 0 InfCodeX.** All replay/tests use
the existing seccomp IPv4/IPv6 denial launcher; new focused tests additionally forbid
Sidecar invocation and semantic-index configuration. Only `_evidence`/packet build
is called, never the real hook call. No dependency installation or real C rerun.

# 20. Next action

Freeze offline evidence and stop after commit/push. Any future frozen reviewer
counterfactual needs separate explicit paid authorization. The current result
justifies the host evidence channel, not a model verdict prediction or a closure
policy redesign.
