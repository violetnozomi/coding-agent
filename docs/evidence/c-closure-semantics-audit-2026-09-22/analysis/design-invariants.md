# Existing design versus observation

Sources: `docs/infcode-alignment-learning-log.md` A257, A259, A264;
`tests/runtime/test_task_contract.py`; `tests/runtime/test_requirement_scope_runtime.py`;
`nz_coder/runtime/verification/sidecar_verifier.py`.

- A257 allows relevant targeted verification to satisfy behavior candidates, rejects
  unrelated/static checks, and invalidates evidence after relevant mutation.
- A259 explicitly chooses a conservative zero-call fallback when an exact command
  exists; valid planner output can supply a richer contract. There is no historical
  requirement that every retained spec sentence becomes a typed requirement.
- A264 requires semantic_review for compatibility, including planner contracts that
  omit it. Its tests explicitly leave compatibility candidate after exact test pass.
- `satisfaction_mode` is read during deterministic docs/artifact mutation closure.
  `observe_verification` does not branch on that field. `required_evidence` is the
  actual semantic-review gate. Semantic/mixed/deterministic behavior without required
  review all close on a qualifying exact pass. Redefining these modes globally would
  change product policy, not repair a demonstrated violation of their tested contract.
- General semantics of the word semantic are underspecified. A broad behavior
  satisfied status is not empirical proof of every natural-language clause. The
  semantic certification prompt explicitly warns that exact pass proves execution
  only. This tension remains, but does not justify inventing a mandatory-review rule.
- Existing same-generation revise/accept production tests allow the final report to
  change without speculative source edits. A new accepted real verdict can close R2;
  fail-open provider errors cannot. This audit does not change that ownership.

# Proven invariant violation

`_diff_sections`: "stable per-file sections".
`_bounded_diff_hints`: "bounded per-file unified diff evidence".
`ChangeTracker.render_current_diff`: the production producer consumed by
`SidecarVerifierHook._evidence`.

Producer emits `## path` + difflib `---/+++` sections; consumer formerly split only
`diff --git`. Multiple paths therefore selected the same whole changeset. A parser
sentinel appeared under writer and vice versa, and duplicate prefixes exhausted
the budget. This is deterministic host evidence misattribution, independent of any
LLM opinion. A real ChangeTracker test and frozen C diff fail before the fix; Git
format is the passing counterexample. Only section parsing is changed.

The historical writer body does appear elsewhere inside repeated chunks. The bug
does not prove all relevant code was absent, nor that it caused the final accept.
The unchanged store module is not supplied as source context in this normal review.
