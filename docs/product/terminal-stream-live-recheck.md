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
