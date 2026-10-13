# Frozen initial results (before product changes)

Baseline core: `89124f9870e61a38290b1af1c97b0529da7188bb`; installed wheel.
Frozen 2026-09-05, after all four scene invocations and before any product edit.

- T01: headless launch exit 3 (missing credentials), before any provider dispatch.
  T01-T04 real model tasks NOT_RUN; paid requests 0, paid spend 0. Independent
  baseline acceptance FAIL for all four targets; visible compatibility tests pass.
- First F01/F02 harness attempt: F01 actually denies/approves/re-prompts correctly,
  but every settled HTTP run becomes failed. F02 is inconclusive in this attempt:
  consecutive user turns were coalesced after failed settlement, and the scripted
  transport incorrectly selected the earlier F01 marker. Corrected only fixture
  command selection; all original private output retained.
- Complete initial matrix on unchanged core: F01 rejects without writing, once
  writes, repeated operation asks again, dialog visible; server final failed.
  F02 actual slow-tool PID disappears after formal abort, late file absent,
  cancellation state and UI cancelled; next request in same session fails.
  F03 one client termination while server running, task finishes its real tool,
  reconnect stays responsive, one user request, but server final failed.
  F04 3,000-line bounded output, exit 7 error tail and final text visible, captured
  terminal 19,298 bytes, widths 100/50/110; no recognizable truncation disclosure,
  server final failed. Layout visual acceptance NOT_VERIFIED (PTY only).

Confirmed A candidate: installed public SDK succeeds without a supplied event
bus; actual HTTP run fails in AgentRunner._typed_result at deepcopy(context.metadata).
The live event publisher stored in metadata reaches a subscriber's TextIOWrapper,
raising TypeError after the runtime emitted completion. Public error redaction
works but the terminal/HTTP final state and persisted history diverge. Diagnostic
traceback captured only under offline dummy credentials.

B candidate: long tool output is collapsed but no omission disclosure was visible.
Needs source/PTY inspection before calling this a confirmed defect.

No model quality (C) conclusion is possible. D: this execution environment has no
available credential; no secrets requested and no trust checks bypassed.

Post-freeze evidence audit (original private records unchanged): F04's initial
`tail_visible=true` was a faulty observation, not a valid success. The marker
appears in the echoed shell command; actual tool card says only "Tool execution
failed." The scripted final tail has stdout lines 2997-2999 (buffered stdout can
follow stderr). Critical error-tail preservation therefore NOT_VERIFIED, and
missing output/truncation disclosure remains a B usability gap. The initial F02
`cancel_latency_seconds` includes reuse; it must not be reported as pure abort
latency. Future captures separate cancellation settlement and reuse duration.
The cumulative `final_visible` substring also includes earlier runs and is not
independent F04-final evidence; final state is taken from the authoritative
snapshot and transcript, not that Boolean.
