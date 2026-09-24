# Constructor/precondition implementation prerequisite audit

Classification: **PRECOND-EVIDENCE: DESIGN-NOT-IMPLEMENTABLE** under the current
selection contract. This is a policy-design blocker, not proof that no future
bounded implementation is possible. No production fix was made.

Baseline: ee26ac5410e63206a2c2e079629e996ba651bff5.
All experiments were local under the existing offline socket-denial launcher.
No main, verifier, planner, embedding or InfCodeX requests were made.

## Genuine RED

`red/focused-tests.txt` records **2 failed / 3 passed**. The two failing tests
first establish a ready structural index and a naturally resolved class callee,
then fail specifically because production Sidecar lacks its constructor envelope.
The generic fixture locates Payload. The C fixture uses historical changed paths,
not a hard-coded model lookup, and naturally locates Config. The baseline C packet
still has the full authority, writer diff, unchanged save body and 46-pass fact.
No hidden acceptance result is introduced into the packet.

## Why implementation stopped

The same changed function can call a payload constructor and an unrelated audit
marker constructor. Both edges are high-confidence imported-binding class calls.
Both classes are unchanged implementation source. The proposed Route A predicates
therefore select both. The current contract additionally demands semantic boundary
relevance and exclusion of the unrelated class. Neither a relevance input nor a
validated deterministic relevance rule was specified or proven in the prior audit.
A count limit bounds volume but does not solve that classification problem.

The counterexample does not prove these programs are indistinguishable under all
future analyses. It proves the tested Route A predicates alone are insufficient.
Using return-value/dataflow inference, task-word matching, type guesses, or a new
parser would go beyond the authorized design. Silently weakening the unrelated
class requirement would also change the experiment. Accordingly no collector was
implemented and no GREEN or production security/cache claims are made.

## Correction to prior design evidence

The previous audit.py selected generic classes via ast.walk and classes[:2], not
Repo Intelligence. Its C candidate list was literal and its query used writer-only
changed paths plus explicit Config/loads lookups. It recorded unrelated and dynamic
classes as selected while claiming filtering in prose. Thus its general selection
claim was stronger than its experiment. The existing C class edge itself is real:
the new replay confirms it using actual historical changed paths.

Two additional passing observation tests reproduce flaws in that old envelope
prototype: multiline decorators are omitted; a large body is sliced before a late
__post_init__, leaving a partial apparent constructor view. These are prototype
failures, not newly discovered production regressions. No new parser was added;
the tests invoke the old prototype solely to reproduce its behavior.

## Boundary and next action

P2 remains absent from production supporting evidence. P5 is not newly established.
Existing dependency evidence and cache behavior remain unchanged. Multiline
recovery, safe complete-envelope omission, race guards and composite cache identity
still need implementation/tests after the selector contract is resolved.

Next decide a generic relevance contract: either bounded direct-class-callee
context is explicitly acceptable even when some callees are semantically unrelated,
or identify an already available structural criterion that excludes the counterexample
without a type/dataflow or prose-interpretation engine. Do not run CF-C3 before an
implemented channel passes those tests. No reviewer behavior was measured here.
