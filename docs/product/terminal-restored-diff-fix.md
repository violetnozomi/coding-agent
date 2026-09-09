# Restored Session Agent-change review

_Linux local terminal, 2026-09-10. Status: RESTORED_DIFF_FIXED_AND_INSTALLED_PTY_VERIFIED._

## 📋 Outcome and root cause

Restoring a Session now lets the user immediately review its recorded Agent
changes through `/diff`, without running the Agent again. Real model requests
and added fees: **0 / 0 CNY**. The installed test used only the existing loopback
fixture; its scripted responses are not model-quality evidence.

Started clean on `codex/terminal-product-rc` at
`afebe7b9e26d0e198dc7d08ad54004f1d065ddcf`, retaining product source
`6c416c893c79d425dc997c16193b238de857a9c9` and both prior repairs.
The final successful section of the [real Pro report](terminal-stream-live-recheck.md)
remains `LIVE_TERMINAL_REVERIFIED` on its original version; no live task was rerun.

Confirmed chain: `_resume_session` loads/activates the selected Session and builds
a new ProductRunEnvironment. Its ChangeTracker uses the new tracer run ID in
`session_change_dir(session_id)`, but no JSON exists until a snapshot is recorded.
Previously both controller and CLI compatibility code preferred the tracker
merely because the object existed. Loading its nonexistent file returned an empty
payload, hiding the still-valid earlier manifest. `/status`'s Git section instead
reads current workspace Git status; it is not Agent-change ownership evidence.
Unparameterized `render_latest_diff()` uses current-workdir and active-session
implicit context. That legacy API remains compatible, but neither terminal diff
entry uses it now.

The original real site's sole manifest still has 2 changes, matching its workspace
and run `20260909_233147_0189d7dd`. Its old process is gone: historical in-memory
tracker objects were not available for introspection. The old transcript/source
explain that case; formal reconstruction tests and the new installed PTY establish
the following actual path selection:

| Stage | Session / tracker run | Tracker file | Review source |
| --- | --- | --- | --- |
| A: edit complete, before exit | session ending b0441b3d / 20260910_002935_55fa9649 | Valid, 2 changes | Current manifest |
| B: new process, before selection | session ending 70d2cbc8 / 20260910_003052_21d29175 | Absent | No records for this new Session |
| C: resume original Session | session ending b0441b3d / 20260910_003111_12d447f8 | Absent; tracker exists | Original Session directory |
| D: immediate `/diff` | Same as C | Still absent | 20260910_002935_55fa9649.json |

All four use the same isolated project. Paths follow
`sessions/_artifacts/<session>/runtime/changes/<run>.json`.
Tests inspect actual tracker objects; installed run IDs are from `/status` and
persisted trace/manifest evidence, with paths checked against the unchanged
constructor mapping. The original manifest stays present throughout.

## ⚙️ Read-only semantics and bounded change

One public reader, `state/changes.py:render_session_diff`, serves
`TerminalSessionController.diff` and the slash-command compatibility path.
It scopes the supplied workspace and exact Session, validates tracker ownership,
and uses the existing change-file loader/selection function:

- Valid current manifest wins, including an explicit empty record
- Only an uncreated tracker file falls back to the latest manifest in that Session
- The existing modification-time ordering is retained within that directory;
  empty or corrupt latest records do not trigger a search for older nonempty ones
- No records means no recorded changes; unreadable, malformed, linked or
  mismatched records yield a safe diagnostic, not a fabricated empty history

The loader adds an optional strict-read mode; existing callers keep their default
behavior. No database, migration, tracker backfill, Git-diff fallback, checkpoint
restore, new change-set write, model/tool execution or Undo-ownership transfer.
Legacy manifests establish Session ownership through their directory; workspace
and run fields are checked, as is a Session field when present.

Rendering labels the result as **historical snapshots, including undone edits;
not current disk**. It deliberately remains history after Undo/Redo. A new valid
tracker record supersedes old history; a no-modification turn with no new record
does not erase it. User edits after the Agent are not recomputed into its snapshots.
Added/deleted empty files retain explicit labels; unknown after or unconfirmed
before is displayed as incomplete, never inferred deletion. Control characters
are escaped and the terminal prints Rich Text rather than interpreting markup.
No current-disk conflict comparison or new recovery state is introduced.

## 🔍 Regression evidence

Initial command, exit **1**, three genuine failures on old product code:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q tests/test_restored_diff.py --tb=short`.
Both formal `_resume_session` paths reconstructed a real ProductRunEnvironment
with a non-None tracker whose path did not exist, then incorrectly printed
`No agent file changes recorded.`; the cross-workspace/context case also failed.
These red failures were wrong-source assertions, not import or collection errors.

Final targeted command, exit **0**, **173 passed**:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/test_restored_diff.py tests/test_changes_undo.py tests/test_session_revert.py \
  tests/test_session_lifecycle.py tests/test_cli_commands.py \
  tests/test_terminal_session_controller.py tests/runtime/test_terminal_session_controller.py \
  tests/test_terminal_input.py tests/test_terminal_interactions.py tests/test_headless_cli.py --tb=short
```

Coverage includes two Sessions with newer B, another workspace's same filename,
current-vs-later records, explicit/latest empty, absent, malformed, wrong
workspace/Session/run, symlink, directory, unreadable and malformed-content/path
records. Real ChangeTracker persistence and the production Session restore are
used. A real SessionReverter/owned-write fixture tests Undo/Redo, then a
no-record turn and a new change. Read-only assertions compare workspace files,
Git index and manifests; unrelated and same-file user edits remain untouched.
The long Chinese attachment-path regression is included. Gateway model entry
points fail the test if restore/review tries inference.

Ruff and py_compile on the three changed product files plus the two affected test files:
exit 0. Existing `tests/typecheck/check_tool_contracts.py`: exit 0, production
bindings accepted and both incompatible fixtures rejected. This is the existing
tool contract check, not a claim of whole-project static typing. `git diff --check`
passed. Old 349/7/103 counts and the real Pro task are not counted as rerun.

## 📦 Frozen package and installed cross-process PTY

Product source: **4f4a5af5a035fa81554e0c836ebfb164087f318b**.
Wheel: `.nz-coder-runs/terminal-restored-diff-fix/reviewed/nz_coder-0.1.0-py3-none-any.whl`.
SHA-256: `4b35dbdaa1fb4f3ed269484afad38058d3b004f8ef19a38b2ebdc049f4a66d3a`.
Sibling sdist SHA-256:
`b45984c7e2e1b1a39049c44197453b2a98c67e62687f9581834bfb51d2a0f278`.

```bash
.nz-coder-runs/p1-env/bin/python -m build --wheel --sdist \
  --outdir .nz-coder-runs/terminal-restored-diff-fix/reviewed
python3 -m venv /path/to/new-nz-coder-env
/path/to/new-nz-coder-env/bin/python -m pip install \
  .nz-coder-runs/terminal-restored-diff-fix/reviewed/nz_coder-0.1.0-py3-none-any.whl
```

Build/install/pip-check exited 0. Acceptance used a fresh noneditable
`$CHECK_ROOT/install`; dependency files were copied without hardlinks from the
previous independent install, then the new wheel force-reinstalled with
`--no-deps`. No dependency resolution/upgrade/downgrade masked the bug. The reviewed wheel
was subsequently installed into this same independent environment with --no-deps.
Python 3.13.12, OpenAI 3.10.0, httpx2 2.12.0, prompt_toolkit 3.0.53, Rich 15.0.0.
All 392 installed package files matched the wheel. CLI:
`$CHECK_ROOT/install/bin/nz-coder`; module:
`$CHECK_ROOT/install/lib/python3.13/site-packages/nz_coder/__init__.py`.
No editable install or PYTHONPATH; cwd was the separate temporary Git project.

A preliminary installed check passed before review identified one minor label
regression: a shared renderer also serves current-disk diff APIs. The historical
disclaimer was moved to the Session-review entry only; a new assertion failed on
the preliminary code and passed after correction. The 173-test suite was rerun,
the package rebuilt, and the complete two-process PTY sequence below repeated on
the reviewed wheel and a fresh project/Session. Preliminary artifacts remain
separate. There were 12 controlled responses across both checks, **0 real calls**.

Final real PTY 120×40, ordinary local CLI, default permissions, long Chinese input.
Only network responses were replaced by unchanged
`tests/fixtures/terminal_stream_provider.py`, bound to 127.0.0.1 with fake key.
Clean launch environment retained boundary selftest=false and ordinary 60/600
timeouts; no artificial long wait or real-account credentials.

| Installed operation | Result |
| --- | --- |
| Allow edit_file / write_file / bash | Actual modification, added test file, 1 local unittest passed |
| First `/diff` | Recorded value.py and test_value.py differences displayed |
| `/exit` | Actual CLI exit 0, process ended |
| New CLI process, `/session` selection | Original Session and 11 messages restored |
| First command after restore: `/diff` | Same set ID, file set and before/after content displayed |
| `/status`, `/exit` | Correct Session/workspace; exit 0 |
| Read-only reconciliation | File/index/manifest SHA-256 unchanged; still exactly one manifest |
| Requests | 6 controlled loopback responses during setup; 0 during restore/review; 0 real model calls |
| Terminal / fixture cleanup | Termios restored twice; loopback fixture stopped with SIGINT |

Session: `session-20260910_002935-b0441b3d`.
Original recorded run: `20260910_002935_55fa9649`.
The new process briefly allocated `session-20260910_003052-70d2cbc8`;
resumption correctly selected the original Session rather than this empty one.
Ctrl+L captured a complete redraw after the first restored `/diff`; it was not
needed to obtain the diff or establish state. The fixture's intentional SIGINT
produced KeyboardInterrupt/exit 1; this is not a product failure (both CLI exits 0).

## 🔐 Preservation and limits

Private `$CHECK_ROOT` contains install and retained preliminary evidence; its
`reviewed/` subdirectory contains the final project, isolated HOME, provider log,
two PTY transcripts, review screens, before-review hashes, original-site hashes,
`verify-evidence.py`, launch command and result.json. Its evidence check exited 0;
the [safe result projection](evidence/restored-diff-result.json) records identities,
hashes and outcome without host paths or model bodies.
The 153 files in the original successful real site remained byte-identical:
aggregate SHA-256 `fecf6a776007003649c9341a6d9887ca9a61bc471887eb37c67f3426b3c50bbf`
(sorted relative names and per-file SHA-256). No original Session was activated
or edited; authorization, patch, usage and history remain unchanged.

This closes local recorded-diff review only. It is not cumulative all-run Git
history, live-disk diff, or a promise that historical edits remain applied after
Undo/user changes. Text terminal evidence does not establish full visual, mouse,
clipboard or Windows/HTTP coverage. No new real-model test or fee authorization
is needed for this read-only fix. No changes to verification_state, billing,
model catalog, boundary selftest, P2 or other repositories. Stop here.
