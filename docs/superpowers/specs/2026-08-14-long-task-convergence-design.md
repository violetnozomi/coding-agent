# Long-Task Convergence Verification Design

## Goal

Close TP-025 with real product evidence rather than another architectural
parity claim. A representative long edit must finish in at most 15 model calls
and 25 tool calls, execute the explicit acceptance suite, and retain enough
evidence for an accurate final answer.

## Baseline-First Rule

The Runtime-owned verification contract, Chinese command-boundary fix, and
DeepSeek replay fix materially changed the old `default20-long-20260814`
failure path. Re-run the same class of task before changing context behavior.
If the new run meets the numerical and correctness gates, record the result and
do not add another mechanism.

## Diagnostic Decision

If the run still exceeds the gate, classify the dominant waste from trace:

1. Repeated reads of already-observed files or symbols: add phase-aware evidence
   compaction after the first committed edit, preserving recent turns, failures,
   diffs, and user-declared acceptance evidence.
2. Excessive initial known-location discovery: add bounded first-turn repository
   context for explicitly requested paths, using existing Repo Intelligence.
3. Serial edit/test recovery: improve convergence guidance or deterministic
   acceptance scheduling only when trace proves this is dominant.

Only the first dominant class is implemented in this phase. Reducing the hard
turn cap is not an optimization because it can truncate incomplete work.

## Observed Outcome

The fresh baseline did not match the three original categories exactly: the
dominant pre-edit waste was repeated shell/environment probing after all target
files had already been read. The selected bounded implementation therefore
uses RuntimeState evidence to reject only late pre-edit Bash probes for
modifying tasks with an explicit post-edit verification contract. Subsequent
fresh traces exposed and justified two adjacent robustness fixes: a top-level
single-file `path` fallback for `apply_patch`, and classified recovery for
subprocess package-root/workspace-boundary failures. The yellow acceptance
window moved from 70% to 60%; orange/red remain unchanged.

Two final runs completed every requested layer and passed independent suites
(93 and 105 tests), but the best complete trace was 18 model calls and 28 tools.
The correctness outcome is accepted; the 15/25 performance gate remains open.

## Safety and State

- Preserve the durable transcript; compact only the provider projection or
  replace eligible old tool output using existing persisted markers.
- Never prune test failures, tracebacks, current-generation diffs, or explicit
  acceptance results.
- Keep workspace, permission, cancellation, and Session ownership unchanged.
- Add no dependency or Agent framework.

## Verification

- New behavior begins with a failing unit/Runner contract.
- Focused context, Runner, verification, headless, and provider tests pass.
- Full repository pytest and Ruff pass.
- A fresh isolated real headless task supplies model/tool/token/time counts,
  acceptance output, terminal status, and trace path.
- TP-025 closes only if the real run reaches both numerical gates and passes its
  complete acceptance suite; otherwise its remaining dominant gap is recorded.
