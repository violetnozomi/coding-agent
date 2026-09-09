# Terminal stream timeout boundary repair

Subsequent [authorized Pro recheck](terminal-stream-live-recheck.md) stopped on a
new long-input path error before model dispatch. It did not exercise the repaired
stream with a real Provider; the offline evidence below remains unchanged.

Status: **FIXED_AND_INSTALLED_PTY_VERIFIED**. Real-model calls added: **0**.
No `LIVE_TERMINAL_REVERIFIED` claim. This is a transport/terminal regression fix,
not a coding-quality measurement or a new task evaluation.

## Frozen candidate and installation

Source: `0e4cb157e9d1cfeb1d28c3543edc020542612b1f`, on
`codex/terminal-product-rc`, based on clean `25084c0c2a9ad5d9e3a4e571c455efa8b73f48ad`.
Only the six production modules below, focused tests, and the loopback fixture
changed. Follow-up report commits do not change the installed product.
`NZ_CONSTRAINT_BOUNDARY_SELFTEST_ENABLED` remains false.

Artifacts under `.nz-coder-runs/terminal-stream-timeout-fix/final/`:

| Artifact | SHA-256 |
| --- | --- |
| `nz_coder-0.1.0-py3-none-any.whl` | `c2645013b4996f166eb5db94dfb7c8fd01709d144299c16588f4548959623fc0` |
| `nz_coder-0.1.0.tar.gz` | `46b7c8026d77b9714186c696ac65141915da12b6b9f89c2ef6682993434d47cd` |

Build, installation, and `pip check` each exited 0. No editable installation,
source PYTHONPATH, SDK downgrade, package upload, or invented version number.
Fresh installed Python: 3.13.12; OpenAI 3.10.0, httpx2 2.12.0,
prompt_toolkit 3.0.53, Rich 15.0.0. All six changed installed modules were compared
byte-for-byte with the frozen source, from outside the checkout.

```bash
# In this checkout; use a new destination for your installation.
.nz-coder-runs/p1-env/bin/python -m build --wheel --sdist \
  --outdir .nz-coder-runs/terminal-stream-timeout-fix/final
python3 -m venv /tmp/nz-coder-stream-user
/tmp/nz-coder-stream-user/bin/python -m pip install \
  .nz-coder-runs/terminal-stream-timeout-fix/final/nz_coder-0.1.0-py3-none-any.whl
# Then cd to your ordinary repository:
/tmp/nz-coder-stream-user/bin/nz-coder
```

## Confirmed defects and bounded changes

| Boundary | Confirmed old behavior | Repair |
| --- | --- | --- |
| `model_gateway/stream.py` | `last_activity` was set before yield; synchronous permission/tool handling was charged as Provider idle while the reader itself waited for `consumed` | Start the next idle window in yield's `finally`, before releasing the reader. Keep `started` unchanged. |
| `model_gateway/gateway.py` | Post-tool errors were private metadata and returned a completed model outcome | Allowlist original safe error type and observed HTTP status; attach origin, phase, dispatch-started fact, and guard deadline kind. Return aborted with retained payload/usage and no retry. |
| `execution/provider_stream.py`, `execution/loop.py` | Settlement recreated a RuntimeError from a public message | Pass versioned PublicError directly, retaining settled tool output and partial model usage even when Gateway is aborted. Strip the private stream-error continuation field. Preserve 400/422 diagnostic and context-compaction routes. |
| `execution/services.py`, `protocol/message_schema.py` | Local bridge failures also became strings; projected HTTP identity could be lost | Bridge carries the safe original error; message projection accepts PublicError and its observed status without reading private body/headers. |

Idle means active waiting for the next Provider item, not time executing a yielded
consumer callback. Empty queue polls do not refresh it. EOF after a long local
pause is still EOF. The hard deadline is **absolute**, including local pauses;
it is checked when consumption resumes, not a new asynchronous interrupter of
permission callbacks. Existing tool timeouts, cancellation, and overall limits
remain responsible for local work. Neither streaming nor permissions were disabled.

The new local guard is a TimeoutError subclass; public identity is `TimeoutError`
with `origin=local_stream_guard`, `timeout_kind=idle|hard`, and
`phase=read_stream|post_tool_stream`. A real SDK timeout instead remains
`APITimeoutError` / `provider_transport`. Tool bridge failures preserve their safe
original type with `tool_execution` / `execute_tools`. No status is manufactured
when no HTTP response exists; response-only HTTP status also survives.

`tool_dispatch_started` deliberately does **not** assert that files changed:
permission may have been denied. Actual executions, edits and call IDs remain in
the existing tool parts and snapshots, associated with existing message, Session,
run and interaction identities. Error text says to review tool results and `/diff`,
not that every proposed modification was saved. A temporary network cause does
not authorize replay after tool dispatch. The generic `APIError` message-union tag
is compatibility structure, not evidence of an HTTP error.

Both same-chunk and separate empty-choices usage tails are consumed. No finish
shortcut, duplicate tool dispatch, usage recount, or extra request is introduced.
Post-tool Provider failure records aborted model-call completion and error run
termination. A local tool failure with an otherwise successful Provider stream
can still have a completed **Provider call**; the tool and run failure remain
separate authoritative states, not overwritten by that transport fact.
Tool callback failures precede subsequent read failures; the earlier tool cause
wins. Observer and response-close failures do not replace the first error.

## Historical evidence: what remains unknown

The original live directory exists and was read-only throughout. Its 109 files,
including project, Session, trace, configuration launcher, authorization/scope,
patch, result and independent acceptance, have unchanged aggregate SHA-256:
`f97650064efe92b697aedbdcf5d0f48f803578ca82eeec795e496fbe37378a63`.
The digest hashes sorted relative filenames plus individual file SHA-256 values.

Original Session `session-20260909_205051-b2779cb1`, run
`20260909_205051_fac9be1d`: saved stream tool wait 343000.612 ms and persisted
RuntimeError/public internal error are consistent with the confirmed defects.
No complete original exception or original effective idle/hard settings were
found in the saved records. Source defaults are 60/600 seconds, **not confirmed
historical runtime configuration**. Therefore the historical root cause remains
consistent with, but not conclusively proved to be, this local idle timeout.
No HTTP status or DeepSeek outage is inferred. The first failure and prior cost
estimate are unchanged; the original Session was not resumed.

## Deterministic red and green evidence

Initial tests on the untouched old production source:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/runtime/test_stream_consumer_timeouts.py \
  tests/runtime/test_post_tool_stream_errors.py --tb=short
```

Exit **1**, **7 failed / 10 passed**, 0.80 s. Five failures were 60/343.000612-second
and repeated consumer pauses (tail or EOF); two were TimeoutError/ConnectionError
becoming RuntimeError. These were actual behavioral failures, not import errors.
The other initial cases protected immediate/59-second returns, actual idle, hard,
close, throw and cancel behavior. Later additions are regression protection, not
claimed to have all been independently demonstrated red before implementation.

Final focused regression command (exit **0**, **349 passed**, 19.85 s):

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/runtime/test_stream_consumer_timeouts.py \
  tests/runtime/test_post_tool_stream_errors.py tests/runtime/model_gateway \
  tests/test_streaming_state_consistency.py tests/runtime/test_native_runner.py \
  tests/test_cancellation_safety.py tests/test_tool_cancellation_context.py \
  tests/test_terminal_interactions.py tests/test_terminal_session_controller.py \
  tests/test_session_processor.py tests/test_public_error_boundary_phase3.py \
  tests/recovery/test_tool_ledger.py tests/recovery/test_checkpoint_tools.py \
  tests/test_headless_cli.py tests/security/test_run_control_lifecycle.py --tb=short
```

Additional clean legacy boundary selection: exit **0**, **7 passed / 91 deselected**:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/test_loop_fake.py \
  -k 'streaming or provider_client_error or gateway_observer' --tb=short
```

A broader `test_post_tool_stream_errors.py tests/test_loop_fake.py` command also
returned exit 0 / 111 passed in 139.22 s, but an operator SIGINT was sent while
it was slow in the legacy non-streaming/planning portion. That run is **not counted
as clean verification evidence**; the directly relevant selection above completed
without intervention. No assertions were removed, limits increased, or new skips
introduced to hide a failure.

The Native integration uses AgentLoop's real Native facade → AgentRunner →
ProductionTurnModelRuntime → Gateway → provider_stream → _StreamToolBridge →
PermissionManager → actual edit_file/write_file/bash → Session persistence.
Only the SDK completion boundary and module-local monotonic clock are controlled.
An explicit permission event holds the edit while the clock advances 343.000612 s;
approval then produces a real edit, usage tail, next model requests, created
unittest, actual successful process output and final response. The same path
checks denial, permission cancellation, post-edit SDK-like error and absolute
hard expiry: edits remain, no test/next request occurs after error, and safe
TimeoutError identity survives persisted Session state. Existing cancellation
tests additionally cover tool execution and safe cleanup.

Synthetic exception-text/body/header/URL sentinels do not survive public error
projection. Additional tests cover SDK APITimeoutError, broken observer/close,
first local cause, response-only auth status, same/separate usage tails, EOF and
400/422 routing. These prove implementation contracts, not model reasoning.

Ruff, `py_compile`, `git diff --check` each exited **0** on changed files.
`tests/typecheck/check_tool_contracts.py` exited **0**: production bindings passed;
both intentionally incompatible fixtures were rejected.

## Installed real PTY evidence

Fresh private `$CHECK_ROOT` contains `install/`, `project/`, `home/`, `evidence/`.
Actual paths are supplied in the local handoff; none points to the old wheel or
original failure Session. Installed executable: `$CHECK_ROOT/install/bin/nz-coder`;
module: `$CHECK_ROOT/install/lib/python3.13/site-packages/nz_coder/__init__.py`.

Controlled HTTP server (source checkout, external network boundary only):

```bash
.nz-coder-runs/p1-env/bin/python tests/fixtures/terminal_stream_provider.py \
  --port 18765 --log "$CHECK_ROOT/evidence/provider.jsonl"
```

Actual ordinary product launch, cwd `$CHECK_ROOT/project`, real 120×40 PTY:

```bash
stty rows 40 cols 120
stty -g
env -i PATH="$CHECK_ROOT/install/bin:/usr/bin:/bin" \
  HOME="$CHECK_ROOT/home" LANG=C.UTF-8 TERM=xterm-256color \
  API_KEY=offline-controlled-key API_BASE_URL=http://127.0.0.1:18765/v1 \
  MODEL_PROVIDER=openai-compatible MODEL_ID=deepseek-v4-pro \
  NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS=1 NZ_PROVIDER_HARD_TIMEOUT_SECONDS=600 \
  NZ_CONSTRAINT_BOUNDARY_SELFTEST_ENABLED=false MAX_AGENT_TURNS=10 \
  MAX_OUTPUT_TOKENS=4000 "$CHECK_ROOT/install/bin/nz-coder"
stty -g
```

`deepseek-v4-pro` here is a fixture model label; all HTTP inference traffic was
loopback scripted responses. The real TUI, Native execution, permission selectors,
tool processes, files and Session persistence were not replaced. No evaluator,
headless substitute, real credential or developer environment was injected.

Session: `session-20260909_215551-02c5d158`; existing Session trace file key/run:
`20260909_215551_82e76340`. It contains both sequential interaction records;
the product's existing same-Session trace layout was not changed.

| Actual keyboard operation | Installed result |
| --- | --- |
| Submit Chinese/English request to change value, add/run unittest | Real read_file and edit selector; file still returns 1 while awaiting approval |
| Leave selector open beyond idle, press Enter on Allow once | Edit executed once (UI 12706 ms; stream callback 12781.865 ms); returns 2 |
| Allow test-file creation, then allow bash | Created test_value.py once, real unittest reports `Ran 1 test` / `OK`; callbacks 6731.694 / 8509.769 ms |
| Final answer | Displays successful test statement and `Run completed`, 4 tools, 2 changed files |
| `/diff`, `/status` | Diff agrees with actual edit/new file; same Session accepts commands without a model call |
| Loopback `/deny` scenario; request rejection-only operation; Esc on selector after >idle | `Denied by user`; denied.txt absent, previous file hashes unchanged, no delayed write; callback 9359.126 ms |
| `/exit` | Exit 0; alternate screen/cursor restored; pre/post `stty -g` identical, including echo/canonical settings |

This was one controlled coding scenario plus the explicitly required rejection
operation, not a second coding task or a live model retry. The denial run's final
completion means it handled the denial; its tool remains failed/denied, not green.

Reconciliation: 8 logical responses = 7 main + 1 forced-verdict sidecar, each one
attempt; no retries. Five settled tool records include the denied write. All
eight synthetic usage records are 120 tokens, counted once (960 protocol-test
tokens, **not real spending**). The coding portion has 5 main calls and 1 sidecar;
the rejection portion has 2 main calls. Sidecar accept is scripted wiring evidence,
not proof of review correctness. No running/pending tool part remains after exit.

Private artifacts: selected raw ANSI PTY chunks in `evidence/pty-transcript.json`,
structural HTTP log `evidence/provider.jsonl`, reconciliation `evidence/result.json`,
ordinary project and full isolated Session/trace. Post-exit organizer unittest
also exited 0, but is not substituted for the Agent's recorded test execution.
Text/ANSI evidence is not a full visual-layout audit.

## Limits and stop

No new paid request, Pro health probe, second coding problem, new billing system,
P2 rerun or boundary-selftest A/B. The old manual 5 CNY target was not reused.
Real DeepSeek connection behavior after a long pause is unverified: a real close
or read exception still causes a truthful error, not suppression or replay.
The original failing run remains failed. A repaired live single-task retry needs
separate explicit authorization; it is not required for this offline/PTY handoff.
Remote/Windows, mouse/clipboard, full UI design and unrelated legacy planning
performance were not retested or repaired. No main changes, merge, PR or force push.
