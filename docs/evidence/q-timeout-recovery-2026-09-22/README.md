# 1. Historical observation

Baseline HEAD/origin/main: `4e59fe75032dd32186b73d92b7ad98c955866176`, initially clean.
Its production source is unchanged from `45db8e38a8de58d404b6601a47b2fe5b518f248a`.
Historical evidence is read-only:
[Q trajectory](../post-authority-real-retest-2026-09-21/Q/nzcoder/trajectory.md),
[Q termination](../post-authority-real-retest-2026-09-21/analysis/Q-termination.json).

Request 7: upstream HTTP 200 and complete SSE, original finish_reason=tool_calls;
Bash actually executes, times out after 120 seconds, and materializes
`executed=true, dispatch_failed=true, command_failed=false, permission_denied=false,
timed_out=true, exit=null, cancelled=false`. Runtime subsequently ends error,
without another model request or review. Historical runtime-full.jsonl also records
**step_processor_result=continue** between tool settlement and the terminal error.
The historical private exception stack and effective stream-idle setting were not
published. We do not rewrite that history as if its original stack were available.

# 2. Offline reproduction

**Reproduced a deterministic Core bug capable of this sequence.**

The initial production integration with a 1-second Bash timeout and 60-second
Provider idle budget passes on the baseline. Timeout alone is not sufficient.
Changing only Provider idle to 0.2 seconds makes the same test fail: the tool is
settled, the bridge returns continue, but the next Provider request never occurs.
The real Bash timeout is the existing supported minimum, not a fake exit=124.

Production chain retained:
NativeSDKRunner -> AgentRunner -> ProductionTurnModelRuntime -> real streaming
Gateway/projection -> _StreamToolBridge -> ProductionToolRuntime -> real Bash /
ToolExecutor -> ProductionToolResultProjector -> SessionProcessor -> hooks,
recovery, RuntimeState, durable Session checkpoint -> Runner.
Only the Provider boundary is scripted; network connections are rejected. Tests
run under the existing seccomp launcher so children cannot use IPv4/IPv6 either.
No guardrail/hook/recovery/checkpoint/stream path is disabled to obtain GREEN.

Initial RED was recorded before production modification. The final regression
matrix was additionally run with the exact baseline iterator loaded into an
isolated test process using baseline_check.py, without reverting the worktree.
All other production files are unchanged. Result: **6 failed / 10 passed**.
After the six-line fix, the same matrix: **16 passed**.

# 3. Exact root cause

The bug is **Provider deadline ownership**, not a timeout ToolExecutionResult
being raised by a tool callback.

```
Bash timeout -> canonical failed ToolExecutionResult
  -> tool projection / durable message / hooks / recovery
  -> SessionProcessor returns continue
  -> _StreamToolBridge returns continue (no exception)
  -> stream iterator resumes after yielding finish chunk
  -> local consumer/tool wait counted as Provider idle/hard time
  -> TimeoutError in model_gateway/stream.py:67 (idle), :65 (hard)
  -> Gateway side_effect_committed branch: completed + private stream_error
  -> provider_stream._settle_stream_tools: post_tool_stream_error
     and assistant_error_from_exception(RuntimeError(...))
  -> Runner error finalization; next model turn never requested
```

The producer intentionally waits on `consumed` while the consumer processes a
chunk. Thus no Provider progress can be requested during this interval, yet the
old clocks continued charging that local work to the Provider. Even a completely
successful slow tool can cross this boundary; timeout is not the defining property.

Exact new offline stacks, including Gateway caller and throwing frame:
[baseline idle stack](before/final-regression/test_successful_streamed_tool_1/stack.txt),
[baseline hard stack](before/final-regression/test_successful_streamed_tool_2/stack.txt).
The callback receives the real canonical result in the accompanying replay.json.
There is no StreamToolExecutionFailed in this timeout chain. A separate injected
infrastructure-hook failure does produce that exception and remains fatal.

This is strong evidence for the demonstrated Core bug and a mechanism consistent
with historical Q (120-second local wait versus the source default idle=60).
It is not recovery of Q's missing private stack or proof that no other historical
condition contributed. Additional Q lifecycle state is unnecessary to reproduce
this minimal defect; we did not rerun or repair Q.

# 4. Why timeout should be recoverable

ToolExecutionResult already expresses the local execution outcome; failed tool
projection appends a tool-role message and settles a ToolPart with error status.
SessionProcessor.process_result returns continue unless an explicit blocker or
fatal condition applies. The historical Q event and offline baseline with a longer
idle budget both confirm this contract. A completed local failure must not become
a transport error merely because processing its result took time.

Tests assert the actual next API messages contain the matching call ID and bounded
timeout text. They reload the durable Session and verify the tool feedback and
error ToolPart. Existing failed ToolPart projection does not persist the full
canonical timeout metadata; that pre-existing representation is unchanged. The
canonical ToolExecutionResult still retains timed_out/executed/exit/cancelled facts.

# 5. Architecture change

Only production file changed: `nz_coder/runtime/model_gateway/stream.py`.
On resuming from a yielded chunk, subtract the local consumer interval from both
Provider idle and hard clocks, before releasing the producer's backpressure.
Actual Provider waiting remains timed, with the same configured budgets. This
changes which component owns elapsed time, not the budgets or fatal policies.

Added tests:
- `tests/runtime/model_gateway/test_stream_timeout_ownership.py`: deterministic
  clock-level idle/hard RED, genuine producer timeout, cancellation distinction.
- `tests/runtime/test_streamed_tool_failure_domains.py`: Native production
  integration across recoverable/fatal/cancel domains, durable/model-visible
  feedback and real verification rerun.

No Runner/bridge/tool schema/permission/completion/review changes. No exception
catch-and-continue, error-string heuristic, or timeout exit-code fabrication.
The implementation patch and source hashes are saved alongside this report.

# 6. Safety properties preserved

| Boundary | Regression evidence |
|---|---|
| Recoverable timeout | Default, short idle and short hard: real timeout facts unchanged, next model turn and durable tool message |
| Nonzero Bash | exit 7 remains command_failed, next turn receives failure |
| Permission denial | denied invocation remains not executed; model sees denial, no Provider error |
| Successful tool | normal output/continuation unchanged |
| Provider transport exception | ConnectionError after tool stream remains fatal; no next request |
| Tool infrastructure exception | Real after_tool_result hook raises RuntimeError; StreamToolExecutionFailed remains fatal |
| User cancellation | Native run returns cancelled, no normal next request, no fabricated timeout |
| Genuine Provider delay | Both idle and hard timeouts still raise for a stalled producer |
| Verification timeout | No passed contract or current-generation verification; requirement completion is not granted |
| Real verification rerun | Separate committed file write, real timed-out pytest, real successful pytest: only the successful rerun binds current generation |

Verification fixtures are dedicated temporary files. They do not alter the Q
fixture or Q final files. One fixture always sleeps; the rerun fixture records an
attempt and only its next genuine subprocess test completes. No ledger/evidence
is prefilled. Tests inspect state before that rerun as well as after it.

# 7. Tests

Published logs normalize host paths and trailing whitespace; exception types, messages, frames and test results are retained.

All exact commands, exit codes and elapsed times are in [commands.jsonl](commands.jsonl).
`check.py` records actual subprocess status (including failures), not a pipeline's
last command status. Earlier exploratory shell commands displayed pytest logs
with cat; their pytest result is explicitly marked retrospective in the index.

| Check | Result |
|---|---|
| Baseline simple Native timeout | 1 passed |
| Baseline short-idle integration | 1 failed, 1 passed |
| Baseline clock ownership | 2 failed, 3 passed |
| Final matrix using exact baseline stream | 6 failed, 10 passed; expected RED |
| Final focused fixed matrix | 16 passed |
| Broad first pass | 1 failed, 1724 passed, 11 skipped; newly added test fixture issue described below |
| Broad final | 1725 passed, 11 skipped (242.04 seconds of pytest execution) |
| Ruff whole repository | exit 0, All checks passed |
| compileall nz_coder | exit 0 |
| CLI --help under network denial | exit 0 |

Focused command:

```
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_streamed_tool_failure_domains.py tests/runtime/model_gateway/test_stream_timeout_ownership.py
```

Broad command:

```
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime tests/security tests/test_runtime_state.py tests/test_sidecar_verifier.py tests/test_message_schema.py tests/test_task_policy.py tests/test_loop_fake.py tests/test_verification.py tests/test_verification_planner.py tests/test_run_evidence.py tests/test_reviewer.py tests/test_permissions.py tests/test_stall_detector.py tests/test_stall_sidecar.py tests/test_edit_recovery.py tests/architecture tests/test_streaming_state_consistency.py tests/test_session_processor.py tests/test_cancellation_safety.py tests/test_recovery.py tests/test_tool_cancellation_context.py
```

Test development failures were not assigned to historical baseline:
- The cancellation observer initially watched a replaced executor instance; it now
  observes the real executor class while still invoking production execution.
- A read-only verification fixture correctly did not bind mutation generations.
  The replacement uses a real committed write in its own earlier turn; putting
  it in the failing batch correctly rolled the write back.
- A fixture-construction error treated a normalized tool-call dict as an object;
  fixed in test code only.
- Natural/max-turn completion can run the declared command through the existing
  Runtime verifier. A once-only sleeping fixture therefore genuinely passed on
  that second run. The timeout-only case now times out on every run, using the
  supported 1-second Bash test setting; the rerun case separately verifies success.
  Assertions were not loosened and production verification semantics were not changed.

The project has no configured mypy/pyright gate. Existing Ruff, compile/import
checks and the relevant Core/security regression set are used; no dependency added.

# 8. What was NOT fixed

- Q README omission.
- Q model-generated circular-wait tests.
- Q review_run_evidence efficiency or its unobserved post-timeout model trajectory.
- General Agent coding quality or overall success rate.
- The loss of private historical exception detail in Q capture.

No paid Q replay, new benchmark, request-budget increase, permission relaxation,
semantic-review weakening, or broad Core refactor was performed.

# 9. Paid calls

**0 paid model requests.** No real Provider, auxiliary verifier, embedding,
InfCodeX, or online evaluation. Controlled Provider calls are local scripted
objects; the validation launcher blocks network access in children as well.
Git transport is used only for the requested repository push.

# 10. Next action

Stop after offline validation and push. The demonstrated deadline-ownership bug
is fixed, preserving the model's opportunity to repair a materialized local tool
failure. Whether the real model would use that opportunity to fix Q's tests,
README or reach efficient review remains unproven and requires a separately
scoped, explicitly authorized future real run. No such run is started here.
