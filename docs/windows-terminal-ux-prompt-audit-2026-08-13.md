# Windows + Terminal UX Prompt Audit

> Continuation note: item 20's historical Tier B disposition was superseded by
> the protected Windows DACL implementation and native verification owners in
> `windows-tui-rc-closure-prompt-audit-2026-08-13.md`.

This is the final requirement-by-requirement audit of the 109 numbered sections
in the 2026-08-13 phase prompt. “Implemented” means source and local contracts
exist. “Windows CI” means only a native `windows-latest` result can supply the
remaining host evidence. “Deliberate boundary” is allowed by the prompt and is
not silently presented as implemented.

| Prompt section(s) | Disposition | Evidence owner |
|---|---|---|
| 1 | Implemented: Agent/Core runtime remained frozen; changes are platform and presentation adapters | source diff and architecture plan |
| 2 | Implemented: fresh POSIX-assumption scan and risk map | `docs/windows-compatibility-risk-map-2026-08-13.md` |
| 3 | Implemented: per-capability A/B/C report; native claims remain gated | `platform_capabilities.py`, platform tests |
| 4, 5, 6, 7, 8 | Implemented: explicit PowerShell 7/Windows PowerShell/cmd and Bash/sh argv, encoding and exit semantics; no `shell=True` product path | platform/shell tests, W3 |
| 9, 10 | Implemented: Job Object binding with PID-scoped bounded `taskkill /T /F` fallback | process backend and cleanup tests, W6/W9 |
| 11, 12, 13, 14 | Implemented: conditional pywinpty ConPTY behind one ProcessService; 80x24, 120x40, 200x60 | backend tests, W7/W8 Windows CI |
| 15 | Implemented as three separate controls: TUI Agent cancellation, read-wait cancellation, process Ctrl+C byte input | fullscreen/process tests, W9 |
| 16, 17, 18 | Implemented: Windows drive/case/UNC/space/CJK/punctuation containment contract; no broad pathlib rewrite | Windows path tests, W4/W5 |
| 19 | Audited: `Path.home()/.nz-coder` and workspace state retained; native daemon/install evidence pending | risk map, release smoke, W1/W10 |
| 20 | Honest Tier B: mandatory token authentication/private state, no claim that chmod is a DACL | capability report and final gaps |
| 21, 22 | Existing daemon nonce/endpoint/process-identity fence retained; Windows lifecycle is native CI-gated | daemon tests, W10 |
| 23 | Implemented: Windows PowerShell Unicode text/image adapters and bounded image handling | clipboard tests, W12 |
| 24 | Existing VISUAL/EDITOR and prompt_toolkit handoff retained; configured `code --wait` is supported; native app timing remains W2 evidence | terminal input and capability probe |
| 25 | Implemented: PATHEXT plus `.exe/.cmd/.bat/.ps1` stdio invocation; CI installs and discovers basedpyright, TypeScript language server, and gopls | LSP and platform tests, W14 |
| 26 | Implemented: Windows stdio wrapper resolution, new process group, recursive cleanup | MCP tests, W15 |
| 27 | Dependency retained as default and explicitly probed; CI directly imports every default Tree-sitter language wheel | W1/W14 Windows CI |
| 28 | Implemented workflow with Python/Node/Go setup, real LSP installs, parser imports, product tests, and release smoke | `.github/workflows/windows-product-rc.yml` |
| 29 | Implemented W1–W15 executable manifest | `windows_product_scenarios.py` |
| 30 | Implemented cleanup assertion locally; native zero-orphan result pending | ProcessService tests, W6/W9 |
| 31, 32 | Implemented: terminal-only productization and actual installed PTY audit; prior frictions documented | RC report and release smoke |
| 33, 34, 35 | Implemented: fixed header/transcript/status/prompt regions, compact responsive identity/status | `presentation_tokens.py`, frame tests |
| 36, 37 | Implemented: concise first-use state and `/connect` recovery | frame tests U1/U2 |
| 38, 39, 40 | Implemented: Ctrl+K primary palette, categories, descriptions/shortcuts; slash completion preserved | fullscreen/terminal input tests |
| 41 | Existing bounded `@` completion retained with path/type metadata; relevance remains prefix/contains, not semantic ranking | terminal input tests |
| 42, 43 | Implemented: visible multiline prompt and filename attachment chips | fullscreen and frame tests |
| 44, 45 | Implemented semantic Thinking/Searching/Reading/Editing/Running tests/Waiting/Verifying projection | run renderer and frame tests |
| 46, 47, 48 | Implemented shared cards and semantic text status; restrained existing palette retained | run renderer tests |
| 49, 50, 51 | Existing diff projection retained; large edits default to bounded summary-first Normal mode | timeline/renderer/scenario tests |
| 52, 53 | Implemented risk explanation and scoped choices without policy JSON | interaction tests U5 |
| 54 | Existing structured option/custom/cancel dialog retained | interaction tests U6 |
| 55, 56, 57, 58 | Implemented categorized actionable Provider errors, bounded tool errors, single retry status, no normal traceback | renderer/scenario tests U11 |
| 59 | Existing verification card retained and mapped to Verifying activity | renderer tests U7 |
| 60 | Existing process list/log/kill plus identity/status cards retained | process/remote tests U9 |
| 61, 62, 63 | Deliberate boundary: no nested terminal emulator. Core ConPTY read/write/resize and authenticated Remote controls are complete; raw terminal attach remains a documented product gap | final report gap 3 |
| 64, 65, 66, 67, 68 | Existing Session table/search/status/fork/undo/redo retained; interrupted state has explicit status | timeline, commands, Remote tests U8 |
| 69, 70 | Existing provider/model/reasoning picker with recent/favorite/search retained; no giant unfiltered new list introduced | model/terminal preference tests |
| 71, 72 | Implemented explicit LOCAL/REMOTE identity; Remote remains local-daemon path semantics and does not imply cross-host upload | header tests, final gaps |
| 73 | Core frozen; existing proposal/source/risk/review UX retained | memory command tests |
| 74, 75 | Existing ACTIVE/DISABLED/ERROR/restart-required lifecycle truth retained | extension tests |
| 76 | Custom commands remain inert and only show expanded prompt in debug/runtime messages, not pre-run UI | custom command tests |
| 77, 78 | Existing grouped help retained; Ctrl+K/Ctrl+C footer updated | command/fullscreen tests |
| 79, 80, 81 | Existing themes and semantic token palette retained; status is text plus color | preferences and presentation tokens |
| 82 | Implemented Compact/Normal/Detailed product vocabulary with Normal default and legacy compatibility | preferences/renderer tests |
| 83 | Implemented `<80`, `80–120`, `>120` bands | frame U12 |
| 84 | Windows Terminal, PowerShell, cmd and VS Code host combinations are Windows CI/manual evidence gates; not claimed locally | Windows workflow and final gaps |
| 85, 86, 87, 88 | Implemented CJK paths/chips/output, restrained symbols, textual states, and keyboard-complete interactions | W5/U14 and interaction tests |
| 89 | Implemented logical-frame/golden-style assertions for empty/status/activity/attachments/error/tool states | frame/scenario tests |
| 90 | Implemented U1–U14 manifest | `windows_product_scenarios.py` |
| 91, 92 | Implemented per-run custom-command model override across Embedded/Remote/Headless without global mutation | custom/HTTP/backend tests |
| 93 | Implemented Remote provider/model/mode/permission inspection; run selection remains per-run by design | manager info and Remote status |
| 94 | Implemented authenticated Remote process write/resize with validation and owner enforcement | HTTP/backend tests |
| 95 | Linux fresh isolated install passed; Windows fresh install is CI-gated; macOS remains unverified | release smoke and workflow |
| 96 | Wheel and sdist build/contents/source-external import passed | release smoke |
| 97, 98 | Semantic remains optional; Tree-sitter default; pywinpty has Windows marker | `pyproject.toml` and wheel smoke |
| 99 | Implemented Windows/Linux R1–R12 manifests | `windows_product_scenarios.py` |
| 100 | Completed source-level three-way product comparison | RC report |
| 101 | Explicitly out of scope: account, billing, organization, cloud sharing, hosted marketplace | RC report |
| 102, 103 | Evaluated against ten-step first-user journey; Linux first task is five-minute feasible; Windows conclusion waits for CI | RC report assessment |
| 104 | Measured: 394.078 ms cold installed startup, 336.190 ms warmed median startup, and 12,016-character Markdown rendering at 99.898 ms median / 103.771 ms p95 with 2.783 MiB traced peak; no synthetic FPS claim | release smoke, local performance probe, and stress tests |
| 105 | Satisfied: no animation framework added; existing lightweight text spinner only | source audit |
| 106 | Honest split assessment: Linux RC, Windows Developer Preview pending native evidence | RC report |
| 107 | Executed in requested A→F order with audit/test/implementation/regression gates; final T1–T20 product suite passed 20/20 | implementation plan and test evidence |
| 108 | Satisfied: no Repo/semantic/verification/multi-Agent/memory/marketplace/cloud expansion | source scope |
| 109 | Completed all required report headings | `docs/windows-terminal-ux-rc-report-2026-08-13.md` |

## Re-audit conclusion

All implementation requirements that can be completed on this Linux host have a
source and executable owner. The prompt is not honestly “fully verified” yet:
sections 20, 61–63, 84, 95, and 106 retain explicit boundaries or native-host
gates. Nothing in those rows is represented as silently complete.
