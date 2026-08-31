# Real-Provider Contract Activation Design

## Goal

Make the contract-led runtime active and useful in the default product path, survive malformed or truncated planner output, expose correct nested Python execution facts, and report every Provider call without forcing an extra planning request.

## Design

Runtime bootstraps a conservative `TaskContract` directly from the user request before optional planning. The bootstrap creates bounded behavior, test, documentation, compatibility, artifact, and exact-verification requirements only when the corresponding intent is explicit. It never invents an acceptance command or unsafe path. Optional planner output may replace this contract when valid; invalid JSON retains the bootstrap contract and a deterministic short plan instead of clearing both.

Planner requests use `response_format={"type":"json_object"}` with the gateway's existing compatibility fallback. This reduces malformed output but is not trusted as the only defense. Planning remains opt-in, so default tasks gain contracts without a new Provider call.

ImplementationBundle activation treats a contract with three or more explicit requirements as non-trivial even when the old text heuristic says `simple`. ProjectExecutionFacts detects a single nested Python project, its tests, manifest, package path, and the parent directory required for `python -m package`; all paths remain inside the workspace.

ModelGateway finish observations include purpose, normalized usage, duration, cost, attempts, and finish reason. RuntimeState aggregates these observations by purpose. Run metadata exposes `provider_calls`, `provider_usage`, and `provider_usage_by_purpose`; the existing execution-turn count stays separate. RunResult usage includes control-plane calls exactly once while coding usage continues to be added by Runner.

## Error and Compatibility Rules

- A failed deterministic bootstrap never blocks the run; it logs a bounded diagnostic.
- Invalid planner output cannot erase an existing contract.
- Legacy Markdown planner output may update the plan but cannot erase the bootstrap contract.
- Strict paths remain workspace-relative and public tool interfaces do not change.
- Restored RuntimeState accepts missing accounting fields.
- No external dependency, Agent framework, Git operation, or paid live retest is part of implementation verification.

## Verification

Use TDD for bootstrap, malformed planner fallback, bundle activation, nested execution facts, and purpose accounting. Then run focused Runtime/Loop/ProjectProfile/Headless tests, Ruff, `git diff --check`, and the complete pytest suite. A third paid DeepSeek run happens only after all local gates pass.
