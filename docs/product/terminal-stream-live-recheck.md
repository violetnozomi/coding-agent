# Repaired terminal: first authorized real-Pro recheck

Status: **BLOCKED**, before model dispatch. One installed-terminal launch and one
submission of the requested task occurred. The TUI exited on `File name too long`
while interpreting the long Chinese input as a possible file attachment. No Agent
run, Provider request, tool execution, edit, or added test occurred. No resubmission,
hotfix, alternate input wording, or second task was attempted.

This does not replace either the [original live failure](terminal-rc-live.md) or
the [installed controlled-PTY repair evidence](terminal-stream-timeout-fix.md).
In particular, this attempt **did not reach the 70-second permission wait** and
does not establish `LIVE_TERMINAL_REVERIFIED`.

## Version, installation and authorization

Branch: `codex/terminal-product-rc`; initial report HEAD:
`71a3bcadc5b4fc89ad07b08197f5a2465be41c20`, clean. Product source remains
`0e4cb157e9d1cfeb1d28c3543edc020542612b1f`, with no intervening product changes.
Wheel: `.nz-coder-runs/terminal-stream-timeout-fix/final/nz_coder-0.1.0-py3-none-any.whl`.
SHA-256: `c2645013b4996f166eb5db94dfb7c8fd01709d144299c16588f4548959623fc0`.
All 392 installed package files matched the wheel byte-for-byte before this attempt.
No rebuild, reinstallation, dependency upgrade/downgrade or offline-suite rerun.

The previous independent noneditable installation was reused read-only:
`$FIX_ROOT/install/bin/nz-coder`; imported package:
`$FIX_ROOT/install/lib/python3.13/site-packages/nz_coder/__init__.py`.
Python 3.13.12, OpenAI 3.10.0, httpx2 2.12.0, prompt_toolkit 3.0.53, Rich 15.0.0.
Exact private root paths are in the handoff, not published as configuration data.

Authorization: the user's **“开始”** followed the explicit confirmation covering
one repaired real DeepSeek Pro task, ordinary in-run verification/auxiliary calls,
one approximately 70-second first-edit permission hold, then diff/exit/no-inference
restore. New spending target: **4.824077 CNY**, using the disclosed manual-observation
arrangement. Ordinary TUI has no verified monetary hard admission; output/turn
limits and observation do not guarantee no overspend. The previous target remainder
was authorized for this attempt explicitly, not treated as an account balance.
No P1/P2 grants, balances, reservations or old unknown charges were used.

Only previously approved `API_KEY` and `API_BASE_URL` connection fields were read;
no complete dotenv, developer environment, credential display, health probe, account
query, SDK demonstration or evaluation organizer. Bytecode writes were disabled
to keep the reused installation unchanged.

## Effective configuration and initial state

The installed product's `load_config_snapshot`, `RunSettings.from_snapshot` and
`active_model_selection` were used in a credential-filtered, inference-free check.
The resulting allowlisted projection is saved privately in `config-snapshots.jsonl`.
The ordinary CLI received the same frozen environment; no UI selection changed it.
Because input preparation failed first, there was **no per-run request snapshot or
wire request** to validate beyond startup/submission configuration.

| Setting | Verified configuration |
| --- | --- |
| Provider / endpoint | openai-compatible / official `https://api.deepseek.com` |
| Model / variant | deepseek-v4-pro / null (not explicitly enabled/high) |
| Main turns / output / context | 10 / 4000 / 32000 |
| Provider idle / hard | 60 / 600 seconds; no loopback idle=1 leakage |
| Permissions | default, with no bypass |
| Boundary selftest | false |

Hard remains an absolute deadline including local pauses; the repaired idle
guard excludes consumer time. Neither was reached in this attempt.

The new project was cloned with `--no-hardlinks` from the original timer's Git
repository at initial commit `c967eab9d81fbc21b474c4da19db27f7d07cfa13`, not copied
from the modified working files. Only README.md, duration.py and the original
tests were tracked. No old Session, solution, patch, acceptance script or developer
memory was placed in the Agent workspace. The independent seven-case acceptance
and authorization were frozen outside it before the submission.

Initial checks, exit **0**: original unittest discovery **2 passed**; the hour
feature was demonstrably absent (`format_duration(3600) == "60:00"`). These are
organizer initial-state checks, not Agent verification.

## Sole actual terminal operation and failure

Actual normal launch from `$RECHECK_ROOT/project`, real PTY 120×40:

```bash
stty rows 40 cols 120
stty -g
# Launcher only filters approved connection fields and execve's installed nz-coder.
"$FIX_ROOT/install/bin/python" -B "$RECHECK_ROOT/launch.py"
stty -g
```

The header displayed LOCAL / deepseek-v4-pro / default. The exact user-specified
request was entered once into the interactive input box and submitted with Enter:

> 请修改这个项目的时间格式化函数：输入为非负整数秒数。不足一小时保持MM:SS；从一小时开始使用H:MM:SS，小时不补零，并允许超过24小时。请补充相应测试，实际运行测试，并说明修改及真实测试结果。仅在当前仓库内工作，不安装依赖、不使用子Agent或外部浏览。

The terminal then displayed `NZ-Coder could not start: [Errno 36] File name too long`
with the submitted text appended to the project path, followed by doctor/debug
guidance. The application exited. The raw excerpt is private; the prompt itself
contained no credentials. No second launch was made to enable a traceback.

Allocated Session: `session-20260909_224919-2b58d460`.
Allocated trace/run identity: `20260909_224919_5dc54063`.
Allocation is **not** evidence that an Agent run started: the trace contains only
`repo_intelligence_cold_build` and `repo_intelligence_watcher_started`, with no
`run_start`, `model_call_start`, `llm_request`, `tool_call` or model usage.
No persisted `sessions/session-*.json` conversation snapshot exists.

Read-only source localization on the installed-matching source:
`cli.py` calls `input_ui.prepare_submission(stripped)`; terminal_input.py's
`_dropped_file_attachments` splits text as candidate paths and calls
`_resolve_attachment(..., strict=False)`. Its `path.is_symlink()` check is outside
the OSError handling around `resolve()`. The unspaced Chinese request becomes a
long path component and the displayed OS error matches that path probe. This
localization used source plus the real terminal failure, not another model run.
No product code or tests were changed in this stage.

Structured PublicError `origin`, `phase`, `error_type`, `timeout_kind`: **unknown /
not recorded at this input boundary**. Observed errno is 36; no HTTP status is
claimed. Tool dispatch did not start and no tool completed. This is not evidence
of a new Provider timeout or DeepSeek fault.

The outer terminal shell exited **0** because it ran `stty -g` after the CLI; that
must not be reported as task success. The original CLI exception handler in source
returns 1, but the child exit code was not separately captured, so its measured
exit code is **unknown**. Pre/post terminal mode strings are identical and the
alternate-screen/cursor restoration sequences are present.

## Results, accounting and preserved artifacts

| Required fact | Actual outcome |
| --- | --- |
| Terminal launches / task submissions | 1 / 1 |
| Agent runs / logical model calls / Provider attempts | 0 / 0 / 0 |
| Permission hold / approved edit | Not reached / none |
| Agent added or ran tests / final answer | No / no |
| Main or auxiliary request retries | 0; nothing dispatched |
| Sidecar model/mode/verdict | Not covered; no sidecar request |
| Patch and independent replay | Empty task patch; replay not applicable |
| Seven fixed boundary checks after Agent work | Not run: no Agent work to accept |
| `/diff`, normal `/exit`, no-inference restore | Not reached; application exited on error, no saved conversation to restore |
| Owned processes | Product terminated; no task/tool process started |

New model cost is **0 CNY because no model request occurred**, not because missing
usage was replaced by zeros. Token/cache/reasoning usage is not applicable; no
usage records or bill were created. No product-native unknown price was changed.
For a dispatched request the same-day preserved official Pro peak rates would
have been used conservatively: uncached input 9, cached input 0.30, output 27
CNY per million tokens. No rate request or paid probe was added here.
The original attempt's 0.175923 CNY estimate remains separate; 4.824077 CNY is an
unspent manual target, not new permission to resume after this failure.

Tracked project files are unchanged. Product startup generated `.nz-coder/index/`
with repository graph/index state; those files are retained and are not an Agent
patch. `final.patch` is zero bytes, SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Protected roots were read-only and aggregate content hashes remained unchanged:

| Protected set | File count | Before/after aggregate SHA-256 |
| --- | ---: | --- |
| Original failed terminal attempt | 109 | `f97650064efe92b697aedbdcf5d0f48f803578ca82eeec795e496fbe37378a63` |
| Repaired installed/controlled-PTY site, including reused installation | 7107 | `ff938abf6fec54848454ae69f3b6ec49539d69b13f2070b6c68c18d7e280e099` |

New private site: project, isolated home/startup trace, `scope.json`, allowlisted
`config-snapshots.jsonl`, credential-filtering `launch.py`, frozen `acceptance.py`,
`protected-hashes.json`, `terminal-transcript.json`, empty `final.patch`, and
`result.json`. Evidence reconciliation exited 0: no model/tool events, no saved
conversation, empty patch, terminal modes restored; protected-hash check exited 0.

Only reports and cross-references are committed. Previous 349/7 offline test
results are referenced, **not rerun**. No build, hotfix, release, PR, merge, main
change, second task, alternate-model attempt or automatic resubmission. This first
blocked submission is the terminal result of the authorized recheck stage.

## Subsequent minimal input repair (offline, separate from the attempt above)

Source `6c416c893c79d425dc997c16193b238de857a9c9` moves existing attachment
filesystem probes into the existing OSError/ValueError boundary and removes
duplicate rejection branches. Long prose remains prose; invalid explicit
attachments fail cleanly. Workspace and symlink restrictions remain enforced.
One production function changed: **4 fewer lines**. Parameterizing the existing
attachment test adds 4 lines; total Python line growth including tests is **0**.
No new module, setting, length heuristic or model request.

Old-source regression: `test_terminal_input.py -k attachment_is_workspace_bounded`
returned exit 1, 12 failed / 3 passed (long Chinese/English input and explicit
overlong paths). Repaired regression command returned exit 0, **103 passed**:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q tests/test_terminal_input.py tests/test_terminal_interactions.py tests/test_fullscreen.py tests/test_user_attachments.py tests/test_headless_cli.py --tb=short`.
Ruff, py_compile and diff checks passed on the two changed Python files.

Fresh wheel/sdist build and noneditable installation passed. Wheel:
`.nz-coder-runs/terminal-input-fix/final/nz_coder-0.1.0-py3-none-any.whl`, SHA-256
`a9342669e4e191819c4b5df42c37e620ae9bca1203bdd4b6e82b2b99c0064a44`.
Installed module matched source; SDK remained OpenAI 3.10.0. A new isolated
120×40 real PTY submitted the original Chinese requirement to the existing
loopback fixture: reached the permission selector, rejected the proposed write,
received its scripted final response, and exited normally with CLI exit 0 and
restored terminal modes. Persisted user text equals the original request;
`denied.txt` does not exist. Private evidence is in the new input-check site's
`pty.json`, Provider log and Session, not either original live site.
This proves input-flow repair only; **real DeepSeek recheck remains unperformed**.

## ✅ Subsequent authorized real Pro recheck — 2026-09-09

Status: **LIVE_TERMINAL_REVERIFIED** for the single requested Linux coding flow.
The Agent completed the edit, added and executed tests, and answered after a
measured first-permission hold exceeding idle. This is a new attempt, not a change
to either earlier failure. No product code changed in this stage (zero added
product/test lines), no second task, automatic resubmission, probe or hotfix.
One additional limitation was observed: restored-session `/diff` is empty despite
retained files/history. Thus this is **not** full terminal-RC or release approval.

### Frozen installation, scope and configuration

Clean branch `codex/terminal-product-rc`, starting report HEAD
`5458a8e18aa04dee8e9d11315941b5775e58149b`; product source
`6c416c893c79d425dc997c16193b238de857a9c9`.
No product/pyproject differences between them. Used the input-repaired wheel
`.nz-coder-runs/terminal-input-fix/final/nz_coder-0.1.0-py3-none-any.whl`,
SHA-256 `a9342669e4e191819c4b5df42c37e620ae9bca1203bdd4b6e82b2b99c0064a44`.
All **392 installed package files** matched this wheel before launch.
Reused the independent noneditable `$INPUT_FIX_ROOT/install/bin/nz-coder`;
module `$INPUT_FIX_ROOT/install/lib/python3.13/site-packages/nz_coder/__init__.py`.
Python 3.13.12; OpenAI 3.10.0; httpx2 2.12.0; prompt_toolkit 3.0.53; Rich 15.0.0.
No rebuild or dependency changes for this report-only stage.

The user's **“那就继续之前的工作”** resumed the previously specified one-task Pro
recheck after the separate input repair. Spending target **4.824077 CNY** used
the previously disclosed manual observation/stop arrangement, not monetary hard
admission. Normal iterations and the naturally triggered verifier were included;
no further inference was authorized after completion. Limits cannot guarantee
absolute non-overspend. No P1/P2 budget or unknown reservation was used.

The same credential-filtering launcher execve'd the normal installed CLI, outside
source, in a new initial Git clone and isolated HOME. Only approved connection
fields were read; no credential recording, full dotenv copy, PYTHONPATH or SDK
replacement. The installed product's actual configuration snapshot asserted:

| Setting | Value |
| --- | --- |
| Provider / endpoint | openai-compatible / official https://api.deepseek.com |
| Model / variant | deepseek-v4-pro / null |
| Turns / output / context | 10 / 4000 / 32000 |
| Provider idle / absolute hard | 60 / 600 seconds |
| Permissions / boundary selftest | default / false |

Run-start/model-call events separately confirmed Pro/null, Native streaming,
10 turns and 4000/32000 limits. Idle/hard were verified through installed
`RunSettings` and the unchanged launch environment, not a captured wire request.
Hard still includes local waiting; only idle excludes consumer processing.
Default variant is not an explicit enabled/high setting.

### Actual operation and evidence

Session `session-20260909_233147-37629a24);
run `20260909_233147_0189d7dd`. One real 120×40 PTY launch and exact original
Chinese request (quoted above), entered once. Initial Git commit
`c967eab9d81fbc21b474c4da19db27f7d07cfa13`: original 2 tests passed and
`format_duration(3600) == "60:00"` demonstrated the feature was absent.
No old Session, solution or organizer acceptance file entered the Agent workspace.

| Observed step | Actual result / evidence |
| --- | --- |
| Investigate | list_directory, grep_search, three read_file calls |
| First edit selector | Visible edit_file on duration.py; tracked diff empty before approval |
| Monotonic permission hold | **83.032437 s**, from 1047946.180643 to 1048029.213079; planned ~70 s, longer due to operator/tool scheduling |
| Allow once | edit_file call `call_00_Ty2KEepFQbY8sCzx4NdR4169` executed once |
| Stream continuation | Edit-bearing model call completed with usage after 98.194 s; no idle error, well below hard |
| Add tests | write_file call `call_00_ET_4wVcRycNtad67I3uCB8n2950`; 5 new tests, original 2 retained |
| Run tests | bash call `call_00_ET_YC0FER2wyrXPcQUs7o418603`; `python -m unittest discover -s tests -v`, **7 passed** |
| Final answer | Correctly describes hour format and actual 7-test result; persisted and displayed |
| Sidecar | Real Pro, inherit-main; **accept / verifier_ok**, not fallback |
| Run end | **completed**, 176.568717 s; 8 unique executed tool IDs, no tool failure |
| Review / exit | `/diff` showed both files; normal `/exit`, measured CLI exit 0 |
| No-inference restore | `/session` selected the same ID, 16 messages; `/status` showed correct workspace and two modified files |
| Restore limitation | Additional `/diff` said “No agent file changes recorded.” Files/history/latest change-set remained; not repaired here |
| Final exit / cleanup | Exit 0; pre/post termios equal in both PTYs; no remaining process with task-workspace cwd |

Recorded total edit-tool duration was 92.063587 s including selector delivery and
operator time; this is **not** the measured 83.032437 s hold or inference latency.
Later write/test approvals had 25.210197 / 22.240320 s total tool durations, with
no deliberate second long pause. The Provider stream was not wire-captured:
successful continuation and complete usage are confirmed, not exact tail-chunk
placement. Sidecar's selected frozen code path uses nonstreaming forced verdict,
thinking disabled, no effort override, 1024 output limit; model/verdict are
trace-confirmed, request-mode details are source-backed rather than wire evidence.

Independent substates remain unaltered: `run_end.verification_state=verifying`,
`verification_needed=true`, static stage pending; targeted unittest evidence
is passed. We do not relabel all pipeline stages as complete. Restored diff's
read-only localization is `SessionController.diff()`: an existing tracker takes
precedence over persisted-diff fallback. This observation is not a new repair.

### Patch, independent checks and accounting

[Public replay patch](evidence/terminal-stream-live-final.patch): only duration.py
and tests/test_duration.py, 19 insertions / 1 deletion, no binary or mode changes.
Public zero-context patch SHA-256
`8049308c061ea3e2adaf2264b4fe9dacde3d1be12352076dddeaddf0865cedbf`;
apply with `git apply --unidiff-zero`. This avoids context-only whitespace in
the committed artifact. The private ordinary-context patch SHA-256 remains
`4815684f5b448cdb663c5dbefd2994ae622bb4b615d14837888938042b6f4da8`.
Generated index and Python cache files remain privately preserved, not included
as Agent source changes. Organizer never edited the formal task copy.

On another initial clone: `git apply --check`, `git apply`, unittest discovery
and the frozen acceptance script all exited **0**. Seven independent checks:
0→00:00, 59→00:59, 60→01:00, 3599→59:59, 3600→1:00:00,
3661→1:01:01, 90061→25:01:01. Original test_zero/test_short were also run separately,
each exit 0. Replay files matched the live files. These organizer checks are
separate from the Agent's actual 7-test command, not independent model samples.

| Normalized usage / calls | Count |
| --- | ---: |
| Main / verifier calls | 6 / 1 |
| Actual attempts / retries | 7 / 0 |
| Uncached input | 27,574 |
| Cache-read input / cache-write | 31,232 / 0 |
| Output excluding reasoning | 1,293 |
| Reasoning output | 291 |
| Total tokens | 60,390 |

Seven start/finish pairs, unique tool IDs and run totals reconcile; every call has
usage, all completed on attempt 1. Restore generated **no** model call. No
unknown usage was zero-filled. Frozen same-day official peak CNY/M rates[^1]:
uncached 9, cache-hit 0.30, output 27. Estimate:
`(27574×9 + 31232×0.30 + (1293+291)×27)/1000000 = 0.3003036 CNY`.
Reasoning and verifier costs are included once. Product-native prices remain
**unknown for all 7 calls**, not a zero-cost bill. This conservative estimate is
not an account bill; off-peak pricing may be lower. Manual target remainder
4.5237734 CNY confers no further permission. Original 0.175923 CNY remains separate;
combined terminal estimates are 0.4762266 CNY, not a merged budget grant.

### Preserved evidence and stopping point

New private root alias `$PRO_FINAL_ROOT` contains `scope.json`,
`config-snapshots.jsonl`, `permission-hold.json`, raw PTY transcripts,
isolated HOME/Session/trace, project, replay, final.patch, `reconcile.py` and
`result.json`. Exact private paths are in the local handoff, not the public
configuration. Reconciliation exited 0: calls/usage/tool identities, final answer,
Agent tests, replay checks, original request, exit modes and process cleanup.

Before/after protected aggregates (including the reused install) stayed equal:

| Protected site | Files | SHA-256 |
| --- | ---: | --- |
| Original real failure | 109 | f97650064efe92b697aedbdcf5d0f48f803578ca82eeec795e496fbe37378a63 |
| Stream-fix site | 7107 | ff938abf6fec54848454ae69f3b6ec49539d69b13f2070b6c68c18d7e280e099 |
| Pre-model blocked recheck | 54 | db041c24b339483896908653bfdaf76bf2266605555e4a3149699c65b1b36b57 |
| Input-fix site/install | 7080 | 707ebcd71aef6975d3707cd93a816764688fc136f680664a721d4373e3f28b00 |

The previous 349/7 stream and 103 input regressions are referenced, **not rerun**.
Only reports and safe patch evidence are committed. Text PTY evidence does not
establish full visual-layout coverage. No second task, cancel experiment,
post-restart coding, Windows/HTTP, mouse/clipboard, model ranking, A/B or P2 work.
Stop after this one real task; retain restored-diff and pipeline-display limitations.

[^1]: DeepSeek. Pricing, preserved official source read 2026-09-09, DeepSeek-V4-Pro-0813. https://api-docs.deepseek.com/zh-cn/quick_start/pricing/

The separately versioned [restored-diff repair](terminal-restored-diff-fix.md)
subsequently fixed the empty review after Session restore using local tests and
an installed loopback-Provider PTY only. It added no real model calls or charges
and does not replace this live attempt's source version, result or evidence.
