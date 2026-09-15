# NZ-Coder / InfCodeX paid comparison (2026-09-16)

- NZ-Coder: `2099a0d` (workspace remained clean)
- InfCodeX: `d3a812379b589597347f5be12d5b68477e577f02`
- Model: `deepseek-v4-flash`, one run per side for B and N, maximum 12 iterations/turns
- Execution: real InfCodeX SA CLI and real NZ-Coder `build_product_run_environment` → `NativeSDKRunner`; model boundary was isolated through a parent Unix socket proxy.

## Results

| Task | Side | Model requests | Final status | Changed files | Frozen independent acceptance |
|---|---:|---:|---|---|---|
| B | infcodex | 5 | run.result success=true | `none` | FAIL |
| B | nzcoder | 11 | completed | `catalog/api.py, cli/report.py, mobile/presenter.py, tests/test_catalog_api.py, tests/test_report.py, web/controller.py` | PASS |
| N | infcodex | 12 | run.result success=true | `index.js` | PASS |
| N | nzcoder | 12 | max_turns | `index.js` | PASS |

B had the clearest termination discrepancy: InfCodeX read the repository and produced a plan, then emitted a successful terminal event without modifying the requested API; its frozen acceptance therefore failed. NZ-Coder performed the six-file migration and passed the frozen checks, but its direct test commands were denied by the callback and verification was supplied by runtime-owned verifier tools.

N exercised the requested failure→edit→retest chain on both sides. Both modified `index.js`, passed `node --test literal.test.cjs`, and passed an independent Unicode/metacharacter/type-rejection check. InfCodeX emitted normal success. NZ-Coder reached its twelve-request work limit after verification and reported `max_turns`; the file result is correct, but termination behavior differs.

## Evidence boundaries

Raw request/response bodies, JSONL events, normalized projections, permissions, frozen workspaces, diffs and acceptance commands are in the artifact directory. Token usage is recorded per side; provider billing was not exposed, so no USD amount is inferred. These two single repetitions are evidence about these executions, not a success-rate estimate.

## Previous F calibration

The earlier paid F run is retained separately. It used a non-identical fixture/configuration and had NZ permission-denied Bash attempts followed by verifier execution, so it is not combined with B/N or used as a paired score.
