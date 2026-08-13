# Windows + TUI RC Closure prompt audit

“Done” below means source plus executable local evidence exists. “Windows gate”
means implementation and native test owners exist, but this Linux workspace
cannot supply the required host result. “Boundary” is explicitly allowed by the
prompt and is not represented as partial implementation.

| # | Disposition | Evidence / decision |
|---:|---|---|
| 1 | Done | fresh audit of platform/process/TUI/daemon/MCP source and report matrix |
| 2 | Done | one-shot shell and persistent process share raw-byte decoder |
| 3 | Done | BOM/no-BOM UTF-16 → UTF-8 → configured → Console/OEM/ANSI/locale → replacement order |
| 4 | Done / Windows gate | UTF-8, UTF-16, CP936/932, Japanese, emoji, malformed fixtures; native PowerShell no-BOM owner pending |
| 5 | Done | Job sets KILL_ON_JOB_CLOSE before binding |
| 6 | Windows gate | WC4 proves abnormal parent cleanup |
| 7 | Done | only ProcessService-created PID is assigned; no name scan |
| 8 | Done / Windows gate | ConPTY is Job-bound and uses PID-scoped taskkill when binding fails; WC5 verifies descendants |
| 9 | Windows gate | WC1–WC5 executable with zero-orphan assertions |
| 10 | Done | case, drive, dot-dot, spaces, CJK, UNC, symlink coverage |
| 11 | Windows gate | real NTFS junction read/new-write escape test |
| 12 | Done | lexical plus resolved target/nearest existing parent |
| 13 | Done / Windows gate | protected current-user-and-SYSTEM DACL, fail-before-write Provider credential temp, platform/Doctor A-or-B reporting; native round-trip pending |
| 14 | Done / Windows gate | real `windows-latest` workflow exists; result pending |
| 15 | Done / Windows gate | W1–W15 covers listed product paths |
| 16 | Done / Windows gate | marker dependency, import, ConPTY spawn/resize owners |
| 17 | Windows gate | PowerShell 7 and 5.1 multilingual command test |
| 18 | Done | width-related source paths re-audited |
| 19 | Done | header/chips/tool/process/session clipping column-aware |
| 20 | Done | `clip_terminal_text(text, max_columns)` contract |
| 21 | Done | combining and ZWJ clusters conservatively preserved |
| 22 | Done | useful file labels and `[clipboard image]` |
| 23 | Done | primary surface hides cache path; details stay explicit |
| 24 | Done | Session title, otherwise short ID |
| 25 | Done | workspace basename/short path in header |
| 26 | Done | status/location retained under narrow width |
| 27 | Done | idle/running footer reverified and bounded |
| 28 | Done | provider/no-provider/Remote/interrupted projections covered |
| 29 | Done | real T2 first-provider PTY journey |
| 30 | Done | nine stable product-facing categories |
| 31 | Done | suggested → recent → common → searchable all |
| 32 | Done | `/help` Essentials; `/help all` full catalog |
| 33 | Done | existing tool cards kept consistent; no abstraction rewrite |
| 34 | Done | bounded edit summary and `/diff` full path retained |
| 35 | Done / native edge pending | actionable normal errors; no default traceback |
| 36 | Done | pywinpty absence returns pipe plus install hint |
| 37 | Done | persistent LOCAL DAEMON / REMOTE endpoint state |
| 38 | Done | Attach `!command` disabled with server-safe guidance |
| 39 | Done / Boundary | local daemon paths work; true remote client paths rejected; no upload |
| 40 | Done | logs/write/resize are the stable minimum process interaction |
| 41 | Boundary | raw PTY takeover not merged |
| 42 | Done | recovery risk evaluated; stable controls retained |
| 43 | Done | startup/typing/output/transcript/files/Sessions measured |
| 44 | Done | cached providers and bounded work keep input responsive |
| 45 | Done / Windows gate | T/U/W manifests execute and artifacts persist |
| 46 | Done | scenario/platform/result/duration/failure/environment/version fields |
| 47 | Done | Linux T1–T20 20/20 and U1–U14 14/14 |
| 48 | Windows gate | W1–W15 cannot be replaced by mocks |
| 49 | Unverified | no macOS runner available; stated explicitly |
| 50 | Done | repository contains packaging, CI, scripts, tests |
| 51 | Windows gate | hosted clean image plus isolated release smoke configured |
| 52 | Done | pywinpty Windows marker; semantic dependency optional |
| 53 | Done | installed help/doctor/platform/run/interactive/daemon smoke |
| 54 | Done | 350-match source scan classified; real bugs fixed |
| 55 | Done | Skill hot lifecycle truthful; restart-required remains honest |
| 56 | Done | Core Freeze respected |
| 57 | Done | final Windows A/B/C matrix in closure report |
| 58 | Done | TUI Good/Acceptable matrix; not all Good |
| 59 | Done, scripted | recorded 0 lookup/0 final failure/0 dead end; not called human evidence |
| 60 | Done | cross-platform decision is Developer Preview |
| 61 | Done | 3–5 minute normal-product demo documented |
| 62 | Done | all ten requested report headings present |

## Re-audit conclusion

All Linux-executable implementation and evidence items are complete. Windows
ACL, no-BOM UTF-16, and Job lifecycle owners were added to the native workflow.
The prompt
is **not fully native-verified**: sections 6, 8–9, 11, 14–17, 35, 45, 48, and 51
still require the real Windows artifact; section 49 requires macOS hardware/CI.
Sections 39 and 41 are deliberate, documented boundaries. This is the reason the
release decision remains Developer Preview rather than a false RC claim.
