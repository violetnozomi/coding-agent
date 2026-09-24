# Candidate policy contract — design only

During semantic review the host may attach a bounded amount of current unchanged implementation source for directly returned class targets of changed implementation symbols, when the local call role is trusted Python AST-native metadata and target identity is resolved at confidence >= 0.85.

This is optional supporting repository context, not task authority, permission, an instruction, proof of execution, or a claim of semantic relevance/invalid-state existence.

Require ready/indexed same-workspace consistent generation; changed caller and unchanged target implementation visibility; safe current caller/target bytes; trusted role; unique class identity; AST-derived complete target span. Exclude changed targets, authorities, tests/docs/evidence/oracle/acceptance/generated/vendor/hidden paths using existing policy. Unknown, stale, timeout, ambiguity or unavailable => omit without affecting completion rules.

Deduplicate by stable target symbol_id; sort by (workspace-relative path, symbol_id). No semantic/name ranking. Trace candidates, included/omitted counts and reasons without full source. Future query envelope should reuse existing bounded-query pattern: <=32 changed paths, <=16 candidates, small depth/node/time bounds (existing dependency collector 20 nodes, 50ms graph query, zero wait-for-ready, <=0.5s outer wait). Fail soft on truncation/unavailable; never warm or embed on review critical path. Fixture snapshots use bounded small disposable repositories to audit facts, NOT a production full-repository scan implementation.

Rendering: <=2 symbols, <=1400 chars per complete rendered block, <=2400 total rendered section including label/headers/separators/omission footer. Reserve footer space. Omit whole blocks exceeding either budget; continue deterministic selection among remaining candidates. No partial constructor proof.
