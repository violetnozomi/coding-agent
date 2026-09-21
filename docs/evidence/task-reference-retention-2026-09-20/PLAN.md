# Retain authoritative task references for semantic review

Baseline: e9e24913e419c1dd12ef2e60b745d4422ec6da2a; clean worktree; 0 paid model requests.

Cause: money paid pair HTTP #16/#26 received no original REQUIREMENTS body. Rolling history is limited to 24 messages and verifier transcript rendering does not render tool bodies at all. Reference provenance currently collapses into ordinary context. Revise guidance says ground truth. These are separate producer, transport and authority defects.

Plan (tests first):
1. Production offline regression: genuine named spec read then aged out; assert separate original authority at verifier boundary, not a mutation obligation.
2. Share clause/path classification among mutation extraction, task reference capture and bootstrap artifacts, retaining authority provenance.
3. Bounded immutable original capture through WorkspaceFileAccess at fresh lifecycle bootstrap before model/tool activity. Restore must never re-read current mutable spec as original.
4. Runtime persistence sanitization and canonical successful read metadata/hash observation. No stdout parsing.
5. Independent verifier evidence section and stable cache digest; retain existing compatibility guard. Review findings explicitly subordinate to genuine instructions/specs; supply retained references to main during revision.
6. Focused matrix, production replay, safety/permissions/persistence tests, broader offline runtime regressions; record exact commands/results and limitations.

Record rationale, red/green results and changes here/README; preserve historical evidence. No paid provider, embedding, online evaluation or dependency install. No rolling buffer increase or TaskContract text stuffing.
