# Linux terminal product candidate — 2026-09-09

Status: **TERMINAL_FLOW_OFFLINE_VERIFIED**. Installed-product keyboard/PTY and
controlled-Provider paths were exercised. Real-model acceptance is **not run**
(0 paid calls authorized or sent in this stage). This is not a release, benchmark,
or evidence of model coding quality.

## Candidate and installation

Candidate source: `df41529e358b8a912972b6176d04ab91958a8b3e`, branch
`codex/terminal-product-rc`. It descends from `fb670940634fc99ffcb7807e6d38f625964cf45b`
and includes the prior constraint-boundary implementation and sealed evaluation
work. `NZ_CONSTRAINT_BOUNDARY_SELFTEST_ENABLED` remains **off by default**, including
in the independently installed interpreter. No experimental A/B was performed.
Subsequent report-only commits do not change this wheel's source revision.
Product Git tree: `26e48279e242f2838301cbd786eeea5b25313d54`.

Wheel (local, not uploaded to a package registry):
`.nz-coder-runs/terminal-product-rc/final/nz_coder-0.1.0-py3-none-any.whl`.
SHA-256: `d45b012acd24ab9c062391680c7389af9bcad8b3eee2d74c162b9502fa7a7468`.
The sdist `nz_coder-0.1.0.tar.gz` is alongside it. Version remains `0.1.0`;
the SHA, not a newly invented version number, identifies this candidate.
Sdist SHA-256: `0ea90950c874ad0f89af4955f8c974ef60c72c8b5ed9413ba11c2d9b99edc392`.

Quick start from this checkout (choose a new venv directory):

```bash
WHEEL="$PWD/.nz-coder-runs/terminal-product-rc/final/nz_coder-0.1.0-py3-none-any.whl"
python3 -m venv /tmp/nz-coder-user-venv
/tmp/nz-coder-user-venv/bin/python -m pip install "$WHEEL"
```

Then change into your ordinary code repository, outside the NZ-Coder source tree:

```bash
/tmp/nz-coder-user-venv/bin/nz-coder --help
/tmp/nz-coder-user-venv/bin/nz-coder init
/tmp/nz-coder-user-venv/bin/nz-coder doctor
/tmp/nz-coder-user-venv/bin/nz-coder
```

The package declares Python >=3.10; this run used Python 3.13.12. `init` is optional
if configuration already exists; its refusal to overwrite is intentional. Inside
the application, use `/connect` to select a provider and enter a key in the masked
prompt; `/model` selects the model. **Do not pass `/connect` as a shell argument to
`nz-coder`** (the init hint is imprecise). Discovery may contact the selected
provider even before an inference request. `doctor` is an offline diagnostic, not
a connectivity or account-credit probe.

The header shows LOCAL, repository, model, permission mode and Session. `/help`,
`/keys` and Ctrl+K expose commands. Enter submits; Alt+Enter adds a newline.
`/attach "notes with spaces.txt"` attaches one workspace file to the next request.
Escape dismisses a selector (and rejects a permission request). Ctrl+C clears an
input draft or cancels a running task; wait for cancellation to finish. `/diff`
shows recorded changes, `/undo` and `/redo` restore recorded files, not external
command side effects. `/exit` exits; launch again and use `/session` or `/resume`
to select the saved Session. Review repository identity before continuing.

## Actual installation and entry path

The acceptance root is a private temporary directory, denoted `$CHECK_ROOT` below;
its actual local path is supplied with the handoff. It contains `install/` (fresh
non-editable venv), `home/` (isolated configuration/state), `project/` (ordinary Git
repository), `provider.py`, and `evidence/`. No evaluation fixtures, old chats,
hidden answers, or developer environment were supplied to the tested application.

Verified executable: `$CHECK_ROOT/install/bin/nz-coder`; module:
`$CHECK_ROOT/install/lib/python3.13/site-packages/nz_coder/__init__.py`.
The final three changed installed modules were compared byte-for-byte with source.
`pip check` returned 0. Initial normal dependency resolution installed OpenAI 3.10.0,
Rich 15.0.0 and prompt_toolkit 3.0.53; no dependency downgrade or PYTHONPATH was used.

Actual launch used a real PTY, not piped stdin:

```bash
# Working directory: $CHECK_ROOT/project
stty rows 24 cols 80
env -i PATH="$CHECK_ROOT/install/bin:/usr/bin:/bin" \
  HOME="$CHECK_ROOT/home" LANG=C.UTF-8 TERM=xterm-256color \
  "$CHECK_ROOT/install/bin/nz-coder"
```

`cli.main` → `_run_cli` → `_build_agent` / `build_product_environment` → Native
runtime and `TerminalSessionController`. `TerminalInput` constructs the
`FullscreenComposer` for this TTY; the controller/event projection drives the
transcript, and `TerminalInteractionBridge` drives async permission selectors.
This default **local embedded** path does not require a daemon. `nz-coder attach`
is a different remote entry and was not substituted for it. README's older
"inline" description is not the surface actually tested.

Ctrl+C travels through the composer/input cancellation channel to the existing
run controller and tool cleanup. Slash commands use the existing command handlers;
`/session` selects and rebuilds the saved environment, reattaching the same UI
interaction bridge. No checkpoint or permission persistence rules were duplicated
inside the UI.

Provider configuration was entered through normal `/connect`: OpenAI-compatible,
fake masked key, `http://127.0.0.1:18764/v1`, model ID `deepseek-v4-flash`. That model
name is a **fixture label, not a real DeepSeek response**. Only the external HTTP
boundary was scripted; terminal, Native execution, permissions, tools, files and
Session persistence remained real. No billing monkeypatch or live evaluator was
installed. Fake usage is protocol data only, never an actual cost measurement.
Final structural reconciliation found 23 controlled response rows, including two
non-streaming sidecar calls with forced `emit_sidecar_verdict`. The corresponding
`sidecar_finished` events record `accept` / `verifier_ok`, with reason
`Controlled Provider wiring only`. These scripted verdicts prove connection and
parsing, **not independent review quality**. No extra call was added to force
coverage or change the normal configuration.

## User-journey evidence

Evidence tiers: A = module/contracts; B = installed real PTY (controlled Provider
where applicable); C = real model. All C rows remain unverified. ANSI transcripts
contain redraw deltas, not screenshots: they establish operations and returned
states, **not a complete visual-layout audit**.

| Journey / operation | Result and evidence | Build / tier |
| --- | --- | --- |
| Install, help, initial init; missing key | Starts without traceback; `/connect` guidance; doctor exit 1 for missing credentials | fb67094 wheel / B, no inference |
| Init does not overwrite | Second init exit 1; `.env` hash unchanged; repeated on final wheel | Initial + final / B |
| Configure, model, picker cancellation | Masked fake credential; loopback model discovery; selected model matches recorded request; Escape restores input | Initial + final picker checks / B |
| Mixed input, newline, attachment, resize | Chinese/English and Alt+Enter; attachment present in received request; quoted filename fixed; 80×24 and 120×40 with live resize | Initial + final / B |
| Read/edit/test and follow-up | Existing failing unittest executed (exit 1); subsequent greeting fix executed, 2 tests OK, actual diff matches; same Session retained | Permission-fix wheel / B |
| Final installed coding/file path | Real `apply_patch` modified one file, created another, deleted a third; disk inspected; after restart, terminal `!python -m unittest discover -s tests -v` passed 2 tests | Final / B |
| Allow/reject | Permission selector Allow once executes; Escape rejection left `denied.txt` absent | Permission-fix / B; Allow repeated on final |
| Long output and later input | 120 mixed-language lines; OUTPUT_END; command picker usable afterward | Permission-fix + final / B |
| Running cancellation | CANCELLING → canceled/IDLE; long process gone; delayed `late.txt` absent; later request completes | Permission-fix + final / B |
| Submission during execution | Follow-up retained and processed serially after cancellation; no concurrent writer; full queue timing not exhaustively audited | Final / B |
| Diff / undo / redo | `/diff` shows actual greeting patch; restore warning for external effects; final wheel separately restored create/modify/delete and preserved unrelated user file | Permission-fix + final / B |
| Undo conflict | Concurrent same-file edit → "Refused to undo: file conflict"; disk unchanged; after manually resolving test conflict, undo/redo worked | Final / B |
| Exit and restore | `/session` restored `session-20260909_183516-33d4870c`, messages and current files; follow-up worked; later `/resume` and shell tests worked | Final / B |
| Terminal restoration | Exit 0, alternate-screen exit and cursor restore sequences; before/after `stty -g` identical, including echo/canonical input flags | Final / B |
| Fees unknown | `/stats`: `Cost: unknown`, `Average cost/day: unknown`, model known subtotal clearly labeled | Final / B |
| Sidecar retained | Two controlled calls; forced verdict tool; recorded `accept` / `verifier_ok`, not error fallback | Final / B, scripted verdicts only |
| Real model coding and sidecar quality | **Not run / not covered**, no stage-specific paid authorization | C unavailable |

Final `doctor` returned 0 with honest warnings: isolated state directory mode
0775, missing optional Python LSP, and non-TTY diagnostic invocation. The enclosing
private test root limits access; no real secret was entered. A normal installation
should follow doctor's recommendation to restrict its state directory. Doctor did
not claim successful network connectivity. Initial configuration file SHA-256,
still unchanged at final check:
`729b42fc1072dea68ae26cc3a3c385d1f747c23ab40e5bb8e5513c200c2d6044`.

## Three reproduced roots, no wider redesign

| User symptom | Root / minimal change | Red → green evidence |
| --- | --- | --- |
| First tool waits on raw `Allow? (y/n/...)`; fullscreen confirmation/cancel cannot settle reliably | `runtime/execution/loop.py::set_interaction_askers` updated the current PermissionManager but not `_permission_asker`, which `prepare_run_control` uses for each new run. Persist the injected callback too (one line). | Regression failed with `Permission escaped the terminal selector`; two successive rebuilt controls now return reject/allow via UI; installed PTY shows normal selector and executes only after permission. |
| `/attach "notes with spaces.txt"` rejected an existing file | `interface/terminal_input.py::_resolve_attachment` treated wrapping quotes as literal filename characters. Remove one matching quote pair; retain one-path semantics and existing bounds/symlink checks. | 3 quoted cases failed, unquoted passed; all four now pass, outside-workspace path remains rejected; final PTY attaches 106-byte file. |
| Incomplete cost displayed zero daily/model cost | `state/session_stats.py::render_session_stats` formatted unknown aggregation as numeric totals. Display unknown total/average and explicitly labeled known model subtotal when aggregate cost is incomplete. | Persisted unpriced message failed rendering assertion; priced/unpriced controls now pass; final `/stats` visibly says unknown. No ledger, prices, tokens or stored history changed. |

The initial blocked permission run was retained as errored after explicit process
interrupt and rejection; it was not relabeled success. Its application then exited
normally. Only the private test app was signaled, not other users' processes.

## Reproducible validation

Final source revision was clean before build. Exact build command:

```bash
.nz-coder-runs/p1-env/bin/python -m build --wheel --sdist \
  --outdir .nz-coder-runs/terminal-product-rc/final
```

Build and reinstall returned 0. Initial wheel hash was
`36d609d7fd0c42f02c195be9fdde7560c0f3aeefa2fcd2c6d8d5b7ad33784ec0`;
its results are not used as proof that later fixes work. The intermediate
permission wheel and `1cb93b9` wheel were followed by a fresh final build/install
and the final operations above.

Final targeted regression (exit **0**, **244 passed**, 6.88 seconds):

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/test_terminal_input.py tests/test_terminal_interactions.py \
  tests/security/test_run_control_lifecycle.py tests/test_fullscreen.py \
  tests/test_terminal_backend.py tests/test_terminal_infcode_commands.py \
  tests/test_terminal_session_controller.py tests/runtime/test_terminal_session_controller.py \
  tests/test_headless_cli.py tests/test_cancellation_safety.py tests/test_changes_undo.py \
  tests/runtime/test_native_runner.py tests/runtime/test_constraint_boundary_selftest.py \
  tests/test_provider_connect.py tests/test_session_stats.py --tb=short
```

Ruff and `py_compile` passed on the three modified product modules and their three
test files. `tests/typecheck/check_tool_contracts.py` exited 0: production bindings
passed and both intentionally incompatible fixtures were rejected. `git diff
--check` passed. No skipped assertions, timeout increase, permission disabling or
full-platform test rerun was used to mask these failures.

Private evidence: `evidence/pty-transcript.json` (initial/intermediate operations),
`evidence/final-pty-transcript.json` (1cb93b9 and final operations),
`evidence/final-shell-tests.json`, structural `evidence/provider.jsonl`, and the
ordinary project plus isolated persisted Sessions. Provider logs contain structural
facts, not request headers or complete model prompts. ANSI excerpts can show the
same card during multiple redraws; this alone is not evidence of duplicate execution.

## Limits and next authorization boundary

No observed P0/P1 blocker remains in the exercised local controlled path. This is
not yet `LINUX_TERMINAL_RC_VERIFIED`: real model behavior, live retries/provider
failure behavior, and live sidecar verdicts remain unverified. No mouse/system
clipboard, complete visual rendering, remote/Windows HTTP, all capability
combinations, or remote process recovery was claimed. LSP was optional and absent.
Cancellation leaves a generic tool "Request failed" detail alongside the explicit
run-level canceled status; do not interpret it as a successful tool result.

Only ordinary default permission behavior was used, not a permission bypass.
Filesystem rollback limits and same-file conflicts remain enforced. This stage
did not exhaustively measure queue timing, every invalid configuration, or missing
usage variants. `/stats` is a persisted diagnostic projection, not an account bill
or monetary cap.

Read-only review found no blocker in the three fixes. An adjacent pre-existing
limitation remains: `/detach` does not normalize wrapping quotes. Use
`/detach notes with spaces.txt` or `/detach all`. Review also caught the draft's
incorrect "no sidecar calls" claim; the two controlled calls and actual traces
above are the corrected evidence. This correction did not require a product change.

The terminal entry has no verified hard CNY-budget enforcement for this stage.
Existing model-output/loop limits are not monetary ceilings. Before any real model
work, obtain one explicit authorization covering at most two ordinary coding
requests plus agreed follow-up/cancel/restore actions, model and modes, total cost,
and an acceptable account-side or best-effort stopping arrangement. Do not imply a
hard fee cap that the normal entry cannot enforce. Old P1/P2 grants and balances
are not used; no paid probe or additional billing system was introduced.

Historical tracked `evaluation/` and `docs/evaluation/` have no diff from fb67094.
Protected branch refs still point to fb67094 (constraint enhancement) and 744fba0
(sealed baseline). Original private experiments were not opened for writing; this
stage did not establish a new before/after hash manifest for those private files.
No P2 task, boundary A/B, main merge, PR, release or force push was performed.
