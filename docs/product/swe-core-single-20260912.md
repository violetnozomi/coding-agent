# Native Core single-task result: Astropy 12907

_NZ-Coder · 2026-09-12 · One authorized task, not a benchmark score_

---

## 📋 Result

**Local held-out tests passed.** Installed NZ-Coder, using real DeepSeek Pro,
independently located and repaired `astropy__astropy-12907`. The Agent changed
one source line, reproduced the corrected behavior, ran existing tests, and
returned a final answer. CLI exit code: `0`; run status: `completed`.

This instance is from **SWE-bench Lite**, not SWE-bench Pro.[^1] Pro here names
the model. This is not an official Docker harness result, a pass@1 statistic,
or evidence of an improved overall solve rate. Earlier infrastructure-affected
attempts are retained below, not erased or counted as model reasoning failures.

## 📦 Frozen setup and authorization

| Item | Actual value |
| --- | --- |
| Product source | `c636875216c38bd3c6898c9f341e17dc4a047d49` |
| Product branch | `codex/terminal-product-rc` |
| Wheel | `nz_coder-0.1.0-py3-none-any.whl` |
| Wheel SHA-256 | `9f0be98f8f75b24abb2a8d18926283f39adedac397494f2027a138f5907cd381` |
| Installed executable | `/tmp/nz-swe-venv-20260911/bin/nz-coder` |
| Product Python | 3.13; installed site-packages, no source PYTHONPATH |
| Astropy base | `d16bfe05a744909de4b27f5875fe0d4ed41ce607` |
| Test Python | 3.9.23; existing prepared dependencies, new checkout extensions built locally |
| Provider / model | Official DeepSeek, OpenAI-compatible / `deepseek-v4-pro` |
| Variant | null/default |
| Limits | 20 turns; output 4000; context 32000; idle 60 s; hard 600 s |
| Boundary self-test | Disabled |

The user confirmed one Pro task with a **5 CNY manual spending target**, and
authorized continuation of this same task after the earlier setup failures.
There is no verified monetary hard cap. No second task or paid health probe ran.
Only the previously allowed connection fields were read; requests used direct
network access without inherited proxies.

The normal installed `nz-coder run --output jsonl` entry used the Native Core
(`RunRequest` / `AgentClient` / `AgentRunner`), not the SWE orchestrator and not
the fullscreen TUI. Both CLI and environment requested `auto`; the real Bash
layer nevertheless asked for six confirmations. Each was approved once after
inspection. Thus this run was **operator-assisted for permissions**, not fully
unattended. No response, tool result, source edit, or model decision was supplied
by the operator. The foreground process remained interactive.

Input contained only the public issue and instructions to run relevant existing
tests, remain inside the checkout, and avoid dependencies, external browsing,
sub-agents, historical runs and reference/hidden patches. The official test patch
was first opened after inference ended. No reference solution was inspected.

## ✅ Patch and verification

The Agent replaced the all-ones assignment in `_cstack` with assignment of the
existing right-hand separability matrix. See [the exact Agent patch](evidence/swe-core-20260912/astropy-12907.patch).
There was one successful `edit_file` call, no repeated edit dispatch, and no
other tracked source changes. No new tests were authored by the Agent.

| Check | Result | Exit |
| --- | --- | ---: |
| Organizer preflight: existing `test_separable.py` on base | 11 passed; public nested example reproduced bug | 0 |
| Agent: same test file after edit | 11 passed | 0 |
| Agent: separable + core, piped through `tail` | Output says 70 passed combined | 0 (pipeline) |
| Agent: models, piped through `tail` | Output says 262 passed | 0 (pipeline) |
| Agent: `verify_changed_files` | Changed source compiles | success |
| Organizer: three existing test files, no pipeline | 332 passed | 0 |
| Independent base + official test patch | 2 failed, 13 passed | 1 |
| Independent base + official tests + Agent patch; three test files | 336 passed | 0 |

The two red tests exactly match official `FAIL_TO_PASS`:
`test_separable[compound_model6-result6]` and
`test_separable[compound_model9-result9]`. All 13 official `PASS_TO_PASS`
cases pass after patch replay. The official test patch was validated using
`git apply --reverse --check`; Agent patch replay used `git apply --check`
before application. Verification occurred in a separate clean-base clone,
not by editing the Agent's completed workspace.

Test commands, using the prepared Python 3.9 interpreter:

```bash
python -m pytest astropy/modeling/tests/test_separable.py -q
python -m pytest astropy/modeling/tests/test_separable.py astropy/modeling/tests/test_core.py astropy/modeling/tests/test_models.py -q
```

The final answer correctly describes the source fix and test results, except
that its `test_core.py — 70 passed (combined run)` bullet should be read as
**70 across separable + core**, not 70 additional core cases. The independent
332-case run avoids double counting and does not rely on a pipeline exit code.

## 📊 Calls, cost and retained attempts

Current run: 12 coding calls + 1 inherited-Pro verifier call, 13 attempts,
**0 provider retries**. All 13 starts have finishes and usage; trace totals
agree with final provider totals. Verifier fired for `long-run`, returned
`accept`, trace `verifier_ok`; this was a real result, not error fallback.
Twelve tool calls completed successfully. Elapsed trace time: 263.48 seconds,
including operator permission waits. No owned Core process remained afterward.

| Attempt | Outcome | Calls | Estimated CNY |
| --- | --- | ---: | ---: |
| Initial SWE CLI attempt | SOCKS dependency error before client initialization completed | 0 | 0 |
| Earlier direct Core attempt | Hidden workspace files, then background permission read; stopped, exit 143 | 4 | 0.1786128 |
| Current direct Core attempt | Completed; independent tests passed | 13 | 1.0966680 |
| These attempts combined | Not a bill or new spending allowance | 17 | 1.2752808 |

Current usage: uncached input **102941**, cache-read **90240**, visible output
**1450**, reasoning **3851**, total **198482**, cache-write **0**. The estimate
reuses the previously preserved conservative rate basis in the
[real terminal report](terminal-stream-live-recheck.md): uncached input 9,
cache-hit 0.30, output including reasoning 27 CNY per million tokens.

```text
(102941 * 9 + 90240 * 0.30 + (1450 + 3851) * 27) / 1000000
= 1.096668 CNY
```

Current account discounts/rates and billing were not independently queried.
This is an estimate at that basis, **not an account charge or guaranteed cap**.
Product price remains unknown for all 13 calls; its aggregate `cost_usd: 0.0`
must not be interpreted as zero cost. Earlier terminal experiments are not
included in this task's table.

The previous workspace lived under `.nz-coder-runs`, causing absolute-path
visibility filtering to hide its files. The new checkout has no private-named
ancestor; real listing and search passed before inference. `timeout --foreground`
also prevented the previous SIGTTIN background-terminal stop. No product safety
rule or Agent Core code was changed to make this task pass. Unexpected Bash
prompts despite the requested auto mode remain an observed limitation; this
evaluation does not claim to fix that configuration behavior.

## 🔗 Evidence and limits

Session: `run-20260912_215604-981afda4`.
Run: `20260912_215604_0192ffd4`.
Interaction: `interaction-9166da3ab56043dc9000a326fcaf3a4e`.

Private working scene: `/tmp/nz-swe-core-visible-aqlOPO/`.
Persistent private evidence under the main checkout:
`.nz-coder-runs/swe-core-visible-20260912-aqlOPO/` (mode 0700).
It retains the filtered launcher, manifest, Session/trace, raw CLI output,
stderr, build log, Agent patch and independent test logs. Raw model responses,
host connection configuration and Session content are not committed.

| Evidence | SHA-256 |
| --- | --- |
| `agent.patch` | `d024df6c8d482695a1be15dc75343b38db476fcfd8b8c2c3a004b9dcf77ccfba` |
| `core-events.jsonl` | `b28272d69578dd441e9c918caaff6f05df729db1fb589faa5c9832ceff8b950f` |
| `official-before.log` | `cee0dcaf8f5c4eba0eda77106a84c0af23925d734886ce059719ab1e2c23deee` |
| `official-after.log` | `134450786874e376b486c8e26a418a8c17c0320371f4b9ad67b2bfa67de61be1` |

Old attempts were not resumed or rewritten. This sample demonstrates successful
localization, a minimal correct patch, existing-test execution and normal
completion for this issue. It does not measure new-test authoring, difficult
multi-file task performance, unattended permissions, TUI behavior, Windows,
official Docker reproducibility or SWE-bench Pro performance. No A/B, product
source modification or second task followed. Markdown reporting and
verification-before-completion skills were used to keep claims tied to distinct
Agent and independent evidence.

[^1]: Princeton NLP, SWE-bench Lite dataset: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite
