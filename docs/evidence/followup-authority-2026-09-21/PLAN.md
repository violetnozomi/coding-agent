# Follow-up authority lifecycle

Baseline a04217ce90f1ab67e458ea043642c9c3b16ff94c; initial worktree clean. 0 paid model requests.

1. Audit lifecycle restore/apply-current-round, reference capture/sanitize, shared path roles, canonical synthetic classification, and three remaining Sidecar real-user checks.
2. Add failing regressions for restored follow-up capture, epochs, original immutability, canonical synthetic rejection, budget/restore/cache/observation and production lifecycle ordering.
3. Extend existing bounded registry with authority_epoch + stable source_message_id; initial bind stays one-shot. New genuine instructions capture before tools/persistence; old snapshots never rebuild unavailable historical originals. Newest epochs first; count/byte eviction is explicit and cumulative.
4. Reuse shared classifier including explicit use-path-as-requirements wording, never a filename rule or parallel parser. Unify exactly three Sidecar user-intent checks; keep terminal/verification/fail-open policy unchanged.
5. Run offline focused then runtime/security regressions, retain red/green logs and hashes in this new directory, commit and push after review.

## Findings during implementation

- Baseline reproduction: 11 failed / 2 passed. Existing initial retention works; restored apply-current-round omitted capture. Three Sidecar real-user checks still used raw flag.
- Added source + authority_epoch + source_message_id, newest-first bounded merge, count/byte eviction with cumulative omission; no historical re-read. Production lifecycle identifies genuine follow-ups using canonical helper and existing message identity, including completed-session follow-ups and missing checkpoints. Capture is persisted before the first tool.
- First focused group: 233 passed. Lifecycle/budget/restore expansion: 27 passed.
- Two additional red tests found suffix-as-requirements could cross a later mutation scope, and adapter invalid explicit message could fall back to text. Fixed within shared scope and explicit-message rejection (edges-before: 2 failed).
- Strengthened grounded-history test with a real matching report exposed last_real_user=-1 slicing with no genuine user. Added explicit False in that case; no verifier error/terminal policy changed. replay-final records 3 failures, replay-confirmed 67 passed.
- Actual WorkBudgetController guidance is flagged synthetic in Runner (not XML); dedicated test uses this real builder. Legacy output-limit/completion prefixes now use canonical classification too.
- Broad regression started before the final two narrow fixes; final focused checks validate latest source independently. No real Provider or embedding invocation.

- Final provenance audit: legacy adapter text keeps the real workspace for contract validation but disables authority capture without a canonical genuine message. Added absent-message case; release-focused: 317 passed. Re-running broad on final source before commit.

- Final release-broad: 1588 passed, 11 skipped in 228.16s; release-focused: 317 passed; Ruff and git diff checks passed. Historical evidence audit: 1784 unchanged files.
