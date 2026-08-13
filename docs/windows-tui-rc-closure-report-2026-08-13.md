# Windows Native Validation

This closure re-audited current source and ran every acceptance suite available
on Linux. It does **not** convert Linux simulation into native Windows evidence.
The checked-in `windows-product-rc.yml` job is the owner of the missing native
result and uploads platform, doctor, W1–W15, R1–R12, and performance JSON.

| Capability | Source support | Native Windows evidence | Remaining risk | Tier |
|---|---|---|---|---|
| Agent Core | Canonical product runtime unchanged | W1/W2/W13 job owner; not run here | provider and terminal host combinations | A source / B evidence |
| Files | resolved workspace containment and safe file tools | W4/W5 plus native junction test pending | junction semantics must pass on hosted NTFS | A source / B evidence |
| PowerShell | pwsh → Windows PowerShell → cmd; BOM/no-BOM UTF-16 plus Console/OEM/ANSI decoding | PowerShell 7 and 5.1 multilingual/code-page tests pending | host code-page combinations | A source / B evidence |
| cmd fallback | explicit argv, `shell=False` | Windows workflow pending | quoting edge cases outside fixtures | A source / B evidence |
| Persistent Process | one ProcessService, binary buffer, explicit Job/taskkill/process-group mode | W6 and WC1/WC2 pending | nested host Job policy | A source / B evidence |
| ConPTY | pywinpty default dependency; Job-bound with PID-scoped taskkill fallback | W7 pending | actual GitHub/Windows Terminal behavior | A source / B evidence |
| Resize | backend resize contract | W8 pending | terminal-host propagation | B |
| Ctrl+C | Agent cancel, read cancel, PTY byte input are separate | W9 pending | console-host signal behavior | B |
| Daemon | nonce, token, PID identity, graceful stop | W10 and WC3 pending | native detached-process lifecycle | A source / B evidence |
| Remote | authenticated attach/reconnect proven on Linux | W11 pending | true cross-host upload intentionally absent | A local daemon / B remote |
| Clipboard text/image | PowerShell transports and bounded image persistence | W12 pending | host clipboard availability | B |
| External editor | prompt-toolkit editor handoff | native job pending | GUI/editor installation | B |
| LSP | PATHEXT and wrapper-aware stdio | W14 installs Python/TS/Go servers; pending | third-party server wrappers | B |
| MCP | stdio/HTTP/SSE/OAuth core, wrapper-aware spawn | W15 real stdio child pending | external server compatibility | A source / B evidence |
| Tree-sitter | default parser wheels | Windows imports pending | wheel availability on future Python versions | B |
| CJK path/output | column-safe UI, raw decoding, CJK workspace tests | W3/W5 pending | console font/host rendering | B |
| Workspace security | lexical plus resolved target/nearest parent checks | native junction read/write pending | unusual reparse-point types | A source / B evidence |
| Token security | protected current-user-and-SYSTEM DACL, authentication, Doctor inspection | native ACL round-trip and W10 daemon-token checks pending | filesystem/policy may reject DACL changes and then remains Tier B | A source / B evidence |

Native crash owners are explicit: WC1 normal kill, WC2 Session cleanup, WC3
daemon graceful shutdown, WC4 forced parent termination, and WC5 npm → node
descendants. Every case requires `orphan_process_count = 0`. The tests collect as
18 skips on Linux (native product plus crash cases) and are executed on
`windows-latest` together with pywinpty import/spawn/resize.

# Windows Remaining Risks

1. No successful native artifact is present in this workspace. W1–W15,
   WC1–WC5, and Windows R1–R12 remain release gates.
2. Windows state, daemon tokens, credentials, Sessions, history, preferences,
   and attachment caches now use a protected current-user-and-SYSTEM DACL.
   `platform` reports adapter support and Doctor inspects actual state and `.env`;
   native DACL round-trip/W10 evidence remains pending.
3. Nested Job Object behavior can vary under terminal/CI hosts; the native crash
   suite is the deciding evidence.
4. macOS install, clipboard, PTY, daemon, and attach remain unverified.
5. Cross-host file upload and raw nested terminal emulation are explicit product
   boundaries, not half-implemented features.

# TUI Before/After

| Area | Before closure | After closure |
|---|---|---|
| Width | character-count slicing could misalign CJK/emoji | terminal-column clipping with conservative combining/ZWJ clusters |
| Session identity | long Session ID dominated | title first, otherwise short ID |
| Workspace | long full path in primary frame | basename/short path; full path remains in status/sidebar |
| Header priority | secondary metadata could crowd state | status and LOCAL/REMOTE never intentionally hidden |
| Attachments | clipboard cache filename could leak | `[clipboard image]`; normal files use useful relative labels |
| Palette | registration-order wall | suggested, recent, common, then searchable all |
| Help | every command at once | Essentials by default; `/help all` is explicit |
| Remote | endpoint shown only at attach time | persistent `LOCAL DAEMON` or `REMOTE · endpoint` state |
| Remote shell/files | ambiguous local/server interpretation | `!command` disabled; true remote client paths rejected |
| ConPTY missing | silent pipe downgrade | actionable pipe fallback and `pip install pywinpty` hint |

The footer remains bounded: idle shows send/newline/commands/exit guidance;
running shows Ctrl+C cancellation and queued-request behavior. Tool cards keep
the established title, subject, semantic status, duration, and bounded summary.
Large diffs remain collapsed by default and `/diff` is the full inspection path.

# Usability Findings

| Journey | Rating | Evidence / limitation |
|---|---|---|
| First launch | Good | U1 empty state |
| Provider setup | Good | real T2 PTY journey |
| Prompt input | Good | real T3 coding journey |
| Commands | Good | Ctrl+K palette, categorized help |
| Files | Good | U4/R5 and bounded `@` completion |
| Attachments | Acceptable | local complete; cross-host upload absent |
| Running state | Good | semantic text status and footer |
| Tool activity | Good | U3 renderer |
| Diff | Acceptable | safe collapsed default requires `/diff` for full detail |
| Permission | Good | U5 scoped decisions |
| Question | Good | U6 structured answer/cancel |
| Verification | Good | U7 failure/recovery |
| Errors | Acceptable | traceback-free/actionable core; third-party messages vary |
| Session | Good | T5/U8 durable resume/fork/timeline |
| Process | Acceptable | stable logs/write/resize; no raw screen takeover |
| Remote | Acceptable | local daemon complete; upload/direct shell deliberately limited |

The five-minute test was a strict scripted walkthrough, **not a human study**.
It combined the real T2 provider setup, real T3 unknown-location coding task,
permission/diff/Session-resume scenarios, and the documented demo sequence.
Scripted documentation lookups: 0; final step failures: 0; navigation dead ends:
0. These counts prove reachability, not that a new human will never hesitate.

# Product Acceptance Results

| Suite | Platform | Final result | Notes |
|---|---|---|---|
| T1–T20 | Linux 7.0 / Python 3.13.12 | 20/20 passed | real install, provider setup, coding, daemon attach |
| U1–U14 | Linux 7.0 / Python 3.13.12 | 14/14 passed | structured per-scenario artifact |
| R1–R12 | Linux 7.0 / Python 3.13.12 | 12/12 passed | wheel/sdist and release surfaces |
| Full pytest | Linux 7.0 / Python 3.13.12 | 2068 passed, 21 skipped | 7 known fork deprecation warnings; skips are native Windows only |
| Focused Windows continuation | Linux | 229 passed, 21 skipped | ACL/encoding/Job/daemon/Session/release/architecture paths |
| Ruff | Linux | passed | all files touched by this closure |
| W1–W15/WC1–WC5 | Windows | pending | real `windows-latest` artifact required |
| macOS smoke | macOS | unverified | no runner available in this phase |

The first T run was 19/20 and exposed Remote `Path` shadowing in real attach;
the final run is 20/20 after the source fix. The first R run was 11/12 because
R11 referenced a deleted clipboard test path; R5 had the same stale-manifest
problem before execution. Both owners now point to executable tests, and the
final R run is 12/12. These failures are retained here because they demonstrate
that the suite executes real paths rather than rubber-stamping a manifest.

# Performance

Linux bounded measurements from `benchmark_tui_rc.py`:

| Case | Result |
|---|---:|
| cold CLI startup | 414.732 ms |
| warm startup median | 364.819 ms |
| 10k-file completion median / max | 2.318 / 4.794 ms |
| 100k output projection median | 1.918 ms |
| 1M output projection median / max | 17.479 / 19.481 ms |
| 2000-message transcript median / max | 2.659 / 4.094 ms |
| 10k workspace scan | 126.872 ms |
| 1k Session listing | 2.807 ms |

Final installed T1 startup was 402.687 ms; daemon attach/reconnect were
408.666/390.708 ms; command discovery was 12.324 ms. Fullscreen keeps cached
transcript/status/sidebar projections, Repo indexing stays outside typing, and
large output is bounded before presentation.

# Packaging / Fresh Install

The final Linux release smoke built wheel and sdist, installed the wheel into an
isolated virtual environment outside the source checkout, and ran installed
help, doctor, config, platform, daemon start/status/stop, and terminal startup.
R1 passed at 21.568 s total with installed startup 391.497 ms.

`pywinpty` remains a default dependency only on Windows via a platform marker.
`sentence-transformers` remains in the `semantic-experiment` extra and is not
pulled by default. The Windows workflow starts from a hosted clean image,
imports pywinpty, executes ConPTY spawn/resize scenarios, and repeats release
smoke; that fresh Windows result is pending rather than inferred.

# InfCodeX Product Comparison

Fresh source reading covered InfCodeX measured text, process-tree, Session SDK,
permission, and REPL/TUI ownership. NZ-Coder adopted the relevant contracts:
display-aware width, explicit owned process trees, durable Session title/lineage,
fail-closed command permissions, and bounded terminal state. NZ-Coder's Job
Object kill-on-close is stronger for owned Windows crash cleanup than relying
only on descendant snapshots, but InfCodeX still has a broader terminal engine,
SDK surface, and mature host-specific rendering tests.

No TypeScript/React TUI code was copied into the Python renderer. The useful
semantics were composed behind existing `ProcessService`, `TerminalInput`, and
Session owners.

# OpenCode Product Comparison

OpenCode's PTY owner exposes spawn/data/exit/write/resize/kill, its TUI footer
shows directory/remote/LSP/MCP state, and its command dialog duplicates
`suggested` commands into a leading category before searchable all. NZ-Coder now
matches those user-facing ownership semantics with a smaller prompt-toolkit
surface, and adds recent/common ranking plus explicit true-remote attachment
rejection.

OpenCode remains ahead in visual component depth, remote upload/cloud ecosystem,
and native terminal rendering maturity. NZ-Coder intentionally retains a
smaller component surface and does not claim raw xterm emulation.

# Release Decision

**Developer Preview** for the cross-platform product.

Linux evidence is Release-Candidate quality: T 20/20, U 14/14, R 12/12, fresh
install passed, full regression passed, and no local orphan process was found.
The overall product cannot honestly be called Release Candidate until native
Windows W1–W15/WC1–WC5/R1–R12 artifacts pass. It cannot be called General Usable
without broader native Windows/macOS and real-user evidence.

# Remaining P0 / P1 / P2

- P0: run and archive the native Windows workflow; fix any real W/WC/R failure
  before changing the release decision.
- P1: add macOS native smoke and conduct a real five-minute novice test.
- P2: consider cross-host file upload and raw process screen takeover only if
  product demand justifies their security/terminal complexity.

Core Freeze remains active. Repo Intelligence, verification, memory,
multi-Agent, semantic search, marketplace, and cloud were not expanded here.

# A251 Windows Continuation Evidence

This continuation added the protected Windows DACL, native code-page/no-BOM
decoder coverage, and explicit Job/taskkill/process-group lifecycle diagnostics.
The first full run produced `2066 passed, 21 skipped, 1 failed`; the failure was
the architecture gate detecting a new `state -> runtime` dependency. Moving the
cross-layer ACL contract from `nz_coder.runtime.private_paths` to the neutral
`nz_coder.private_paths` owner fixed the root cause. The final full run is
`2068 passed, 21 skipped, 7 known fork warnings`.

Fresh focused evidence is `229 passed, 21 native Windows skips`. Linux R1–R12
is 12/12; the isolated R1 wheel/sdist install reported 407.788 ms startup, and a
separate fresh release smoke reported 399.619 ms. Ruff, compileall, workflow
YAML parsing, release-doc tests, architecture boundaries, and diff whitespace
checks pass. The local Doctor now verifies both `.nz-coder` (`0700`) and `.env`
(`0600`) as owner-private; the existing `.env` metadata was tightened from
`0664` without reading or changing its contents.

The cross-platform release decision remains **Developer Preview** because no
successful native `windows-latest` W1–W15/WC1–WC5/R1–R12 artifact is present in
this workspace. Source support is closed; native host evidence is still the
release gate.
