# 1. Question

Does corrected per-file diff attribution change the frozen historical C verifier-2
outcome? **This one call still returned accept: CF-C.** This is a frozen semantic
reviewer counterfactual, not a C Main Agent rerun.

# 2. Authorization

The current user explicitly authorized exactly one paid semantic-verifier physical
Provider request, no retry. Actual: **1 verifier, 0 main, 0 planner, 0 embedding,
0 InfCodeX, 0 retries**. No probes or warm-up. The request succeeded with HTTP 200.
An exclusive durable launch marker is created before network I/O; HTTPTransport
retries=0, redirects=false, a one-request event hook, and no retrying SDK/Gateway.

# 3. Historical baseline

Source HEAD: `ac47114affa6556ab8b56e1bb9e1a02171514fc0`; initial main and origin/main
tracking ref equal and worktree clean. Historical verifier-2 physical request 26
returned accept on the C implementation with **46 project tests passed / 11 of 12
independent acceptance checks passed**. The failed direct invalid Config write
can overwrite an existing destination. These facts were rechecked offline on a
disposable copy; the business files, tests and spec were not repaired.

# 4. Frozen inputs

Task, complete original CONFIG_SPEC, retained provenance/hash/observation, final
workspace bytes, native diff source, current Runtime state from historical snapshot
221, R1/R2/R3 contract, ledger, exact verification output, final assistant report,
recent transcript and synthetic review feedback all remain unchanged. Model is
openai-compatible / api.deepseek.com / deepseek-v4-flash; max_tokens=1024,
thinking disabled, stream=false, same forced emit_sidecar_verdict tool and schema.
System prompt is byte-identical to current production and historical verifier-2.

The evaluator's saved workspace.diff is sorted plain unified diff, whereas the
actual reviewer consumed native ChangeTracker output in first-write order. We
reconstructed the latter using production render_change_diff, committed initial
and final files, historical run ID/workspace label and actual_output_paths order.
Applying the old splitter and production hook reproduces the historical user
packet **byte-for-byte: 39,175 characters**. This exact equality anchors the replay.

# 5. Only experimental variable

OLD uses pre-ac47114 diff section semantics. FIXED uses current production
SidecarVerifierHook._evidence to generate file_edit_summary, then production
VerifierContext/build_verifier_user_message to render the packet. Only the rendered
FILE EDITS section changes. No hints, evaluator results, oracle, spec highlights or
obligation checklist are added. Packet-delta.json records all unchanged identities.

**Controlled boundary:** the shared splitter also changes compatibility-delta
additional criteria in an unrestricted current-hook replay. The protocol explicitly
requires additional criteria to remain identical, so we retain historical criteria
and replace only file_edit_summary. Thus this is the requested isolated file-edit
projection counterfactual, **not a full current-hook counterfactual**. That distinction
is material: duplicated historical criteria remain unchanged rather than silently
introducing a second variable.

# 6. Diff evidence delta

| Observation | OLD | FIXED |
|---|---:|---:|
| Listed file sections | 13 | 13 |
| Rendered file-edit section characters | 12,760 | 12,514 |
| Repeated identical-line characters after first occurrence | 6,953 | 1,220 |
| File omission markers | 10 | 4 |
| Writer has its own visible diff | no | yes |

Duplicate characters are an exact line-repetition metric, not semantic redundancy
or token savings. The writer hint is 602 characters, appears at offset 17,317 in
the final user message, and is neither omitted nor truncated. It contains its real
to_dict/dumps v2 serialization diff, not another file's changeset. Both packets
retain the full original bool-exclusion and no-overwrite clauses in their ordinary
authority section. Bounded evidence can still omit later files.

# 7. Real verifier result

**accept / verifier_ok**, HTTP 200, finish_reason=tool_calls, one
emit_sidecar_verdict call. The reviewer says errors, validation/parser, writer,
migrate CLI, existing consumers, tests and docs implement the spec. It cites the
trusted `46 passed in 0.02s`, v1/v2 decoding, v2 serialization, error ordering,
dry-run and CLI behavior. It treats the reported scratch file and packaging limits
as non-blocking. Exact visible request and sanitized response are preserved.

# 8. Defect detection

**No.** The reason does not identify validation of a directly constructed invalid
Config at the writer/save boundary or resulting destination overwrite. Mentioning
validation and serialization separately is not targeted detection. Classification
**CF-C: ACCEPT**. The known business defect remains evaluator-side truth, absent
from the actual request. No second request or prompt repair was attempted.

# 9. Usage

Provider returned: prompt **11,785**, completion **201**, total **11,986**;
cached input **3,584**, cache miss **8,201**. Reasoning-token count and cache-write
count were not exposed. Duration **1.383935535 seconds**. One attempt, zero retries.
**Cost unknown**; no billing returned and no public-price estimate used.
Historical verifier-2 was 11,692 prompt / 295 completion / 11,987 total. These are
observations, not a general efficiency comparison.

# 10. Evidence strength

- Strong: OLD packet byte equality; fixed writer attribution; all non-file-edit
  context unchanged; one real Provider response accepts the known-bad implementation;
  offline fixture remains 46 passing project tests and 11/12 acceptance.
- Moderate: the result is consistent with attribution repair alone being insufficient
  in this reviewer sample. Coarse obligations, evidence coverage and model judgment
  remain candidates; unchanged additional criteria is an explicit experiment limit.
- Insufficient: isolate sampling variation, establish a unique upper-layer cause,
  predict a full current-hook or Main Agent outcome, or generalize reliability.

# 11. What was NOT proven

Not reviewer success rate, Main Agent improvement, a C rerun, a benchmark, proof
TaskContract is sufficient, or proof semantic coverage is generally fixed. No
InfCodeX comparison. Correct diff attribution was a real bug fix, but it does not
guarantee this reviewer detects missing direct-write safety. No production source
was modified in this phase; no additional diagnostic task ran.

# 12. Next action

Freeze and stop. A separately scoped future audit may examine obligation granularity,
semantic coverage and evidence prioritization. This result does not select a unique
fix, authorize model changes or justify adding another sample now.

Offline builder, counterfactual delta, request guard, usage and integrity checks are
in this directory. Commands and real exit results are in commands.jsonl. The single
request authorization has been consumed; do not rerun send-once.

The exact request-visible-user.txt intentionally retains unified-diff blank context
lines (one space). Git whitespace checking flags these 16 payload lines; all other
staged files pass. They are preserved to keep the delivered prompt byte-exact,
with an identical JSON representation and hash, rather than silently rewritten.
