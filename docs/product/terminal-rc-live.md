# Linux terminal — first real-model acceptance

Status: **BLOCKED**. One ordinary coding task was submitted through the installed
interactive terminal. The model changed the implementation correctly, but the
run ended with an internal error before adding/running tests or producing a final
answer. No automatic retry, second coding task, or further paid follow-up occurred.
This does not invalidate the earlier controlled PTY results, nor does it qualify
as `LINUX_TERMINAL_RC_VERIFIED`.

## Scope and actual entry

The user selected Pro, supplied a 5 CNY budget, and then instructed direct start
after disclosure that the ordinary TUI lacks a monetary hard cap. Execution used
the disclosed manual-stop arrangement and existing product limits; 5 CNY was a
budget target, **not an enforced cap**. No old P1/P2 grant or balance was reused.

Product source: `df41529e358b8a912972b6176d04ab91958a8b3e`; report baseline:
`5270e2baf03a16d8174b88955c17cabb4c6592ed`. Same installed final wheel as
[the offline RC](terminal-rc.md), SHA-256
`d45b012acd24ab9c062391680c7389af9bcad8b3eee2d74c162b9502fa7a7468`.
No product source, permission rule, completion gate, sidecar mode or price
calculation was changed during the attempt. Boundary selftest remained default off.

Connection fields only were read from the previously approved main-project
dotenv. They were injected into a clean process environment, not printed or copied
into the project or report. Model: official `deepseek-v4-pro`, endpoint
`https://api.deepseek.com`. The first configuration attempt with variant `high`
was rejected **before inference** because the installed catalog exposed no Pro
variants; the normal supported default was used instead. Do not describe the live
request as explicitly configured enabled/high: variant was null.

Existing configuration limits: 10 main turns, 4000 output tokens per response,
32000 context-token setting. These settings and manual observation are not a
money-admission protocol. No evaluator, monkeypatched gateway, paid health probe,
account query, daemon or SDK demonstration replaced the normal entry.

Private launch command, with `$LIVE_ROOT` denoting the handed-off temporary root:

```bash
# cwd: $LIVE_ROOT/project; real PTY, initially 120x40
stty rows 40 cols 120
/tmp/nz-terminal-rc-qsOm5D/install/bin/python "$LIVE_ROOT/launch.py"
```

The small launcher only filters approved connection fields and `execve`s the
installed `nz-coder` with isolated HOME and normal environment configuration. It
does not issue model requests or call AgentRunner. The task was entered by actual
PTY keyboard input, including an Alt+Enter newline. The UI showed LOCAL / Pro /
default permissions. No old Session, evaluation task or hidden answer was reused.

## Task and observed result

Ordinary dependency-free timer project, initially two passing unittest cases.
Requested change: for nonnegative integer seconds, preserve `MM:SS` below an hour;
at one hour and above use `H:MM:SS`, hours unpadded and allowed above 24; add/run
tests and report actual results. The Agent was told to stay in this repository,
without subagents, dependencies or external browsing.

Session: `session-20260909_205051-b2779cb1`.
Run: `20260909_205051_fac9be1d`.

| Fact | Observed result |
| --- | --- |
| Coding tasks / model calls / attempts | 1 / 4 / 4; no retry |
| Model work | Directory/search/read operations, then one `edit_file` on `duration.py` |
| Permission | Real selector displayed `edit_file: duration.py`; Allow once accepted |
| File change | 4 added lines, 1 removed line; no test edits |
| Run termination | `error`; fourth turn classified `provider_error` after the edit |
| Persisted safe diagnostic | `APIError`, `internal_error`, underlying type `RuntimeError`, retryable=false |
| Agent test execution | None; runtime verification remains unverified |
| Independent replay | Patch applied to a clean clone of the initial timer commit |
| Independent function checks | 7/7: 0, 59, 60, 3599, 3600, 3661, 90061 seconds |
| Original regression | 2/2 unittest cases pass on the replay copy |
| Sidecar | Not triggered in this attempt; no paid request added for coverage |
| Terminal / restore | `/diff` showed saved change; normal exit 0; same Session reopened and `/status` worked without inference |
| Further model follow-up / cancel / restore | Not run after first error; earlier controlled coverage is not substituted for this |

The 7 checks are organizer-authored for this ordinary demonstration, not a hidden
benchmark or independent model-quality measurement. Patch correctness is distinct
from fulfilling the complete request: the Agent did not add tests, execute them,
or give its final explanation. The organizer's post-run checks do not retroactively
mark runtime verification as passed.

Elapsed run time was approximately 355.6 seconds. The final streamed tool wait was
343000.612 ms, including the operator's PTY observation and permission-confirmation
delay. **Do not attribute this interval to model inference latency.** The error
followed this long wait, but temporal association is not a confirmed timeout cause.

The last `model_call_finish` contains usage and `tool_calls` completion, while the
turn settles as `provider_error` and the assistant record carries the safe
RuntimeError diagnostic. The preserved public record does not establish the
original exception message, an HTTP status, or a DeepSeek billing failure. No
speculative Provider root cause is asserted. The generic error display itself
limits diagnosis. Further paid work stopped; this stage did not introduce a fourth
product repair or retry the task to replace the first result.

## Usage and cost

Official [Pro pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
was read by credential-free HTTPS GET before inference. Listed model version:
DeepSeek-V4-Pro-0813. Conservatively use peak prices per million tokens: cache miss
9 CNY, cache hit 0.30 CNY, output 27 CNY. Off-peak rates are half; no account-specific
bill or contract was queried.

Four persisted usage records sum to:

| Normalized token item | Count |
| --- | ---: |
| Uncached input | 17,253 |
| Cached input | 15,360 |
| Output excluding reasoning | 414 |
| Reasoning output | 180 |
| Total | 33,207 |

Peak-rate estimate:
`(17253×9 + 15360×0.30 + (414+180)×27) / 1000000 = 0.175923 CNY`.
Remaining target after this estimate: **4.824077 CNY**, not an account balance and
not permission to continue spending. Reasoning was included once, not added on top
of an output number that already included it.

Product-native price remains unknown for all four calls; its USD aggregate zero
is not a zero-cost bill. The CNY figure above is a manual usage-derived estimate.
The TUI has no evaluation reservation ledger here, so no fictitious reserved or
released amount is generated. Historical unknown charges remain untouched.

## Artifacts and stop

Private `$LIVE_ROOT`: original project and Git initial commit, `home/` with Session
and trace, `scope.json`, `result.json`, `final.patch`, `replay/`, `acceptance.py`,
`terminal-excerpts.json`, `sources/deepseek-pricing.txt`, credential-filtering
`launch.py`. Terminal excerpts are selected ANSI-stripped text; they are not a
complete screen recording. Actual path is provided privately in the handoff.

Verification commands, both exit 0 after patch replay:

```bash
/tmp/nz-terminal-rc-qsOm5D/install/bin/python "$LIVE_ROOT/acceptance.py" "$LIVE_ROOT/replay"
# cwd: $LIVE_ROOT/replay
/tmp/nz-terminal-rc-qsOm5D/install/bin/python -m unittest discover -s tests -v
```

No new build was needed because product code is identical to the delivered wheel.
This follow-on commit changes reports only. History, first attempt, model usage,
patch and error remain preserved. No P1/P2 rerun, force push, main merge, PR, release
or new paid task is part of this handoff.
