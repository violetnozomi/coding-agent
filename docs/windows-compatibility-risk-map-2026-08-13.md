# Windows Compatibility Risk Map

Date: 2026-08-13

This map is generated from a fresh scan of `nz_coder/**/*.py` for `os.name`,
`sys.platform`, signals, PTY/ioctl calls, `shell=True`, chmod/flock, hard-coded
shell names, subprocess creation, and POSIX path serialization. Tier A means a
native implementation is present, Tier B means a documented fallback remains
usable, and Tier C means the capability is unavailable.

| Area | Source assumption found | Risk | Remediation / owner | Tier and evidence |
|---|---|---:|---|---|
| Command shell | `tools/bash.py` and `runtime/process_service.py` previously delegated to `shell=True` | High | `runtime.platform_runtime.select_shell` now chooses PowerShell 7, Windows PowerShell, cmd, Bash, or sh and supplies explicit argv with `shell=False` | A; `test_windows_platform_runtime.py`, `test_windows_shell_runtime.py` |
| Persistent terminal | POSIX `pty`, `fcntl`, `termios` | High | `runtime.process_backends` isolates POSIX PTY and pywinpty ConPTY; pipe fallback remains when pywinpty is absent | A with pywinpty, B fallback; `test_process_backends.py` |
| Process trees | POSIX process groups and signals | High | Process backend binds a Windows Job Object when permitted and otherwise invokes bounded `taskkill /T /F` by PID | A contract, native host proof owned by W6/W9 |
| Path containment | `Path.resolve().relative_to()` assumes host-native paths | High | Shared drive/case/UNC-aware `is_within_workspace`; file tools retain their native `_safe_path` boundary | A; drive, UNC, spaces, punctuation, and CJK unit cases |
| Output encoding | UTF-8-only child decoding | Medium | BOM-aware UTF-8/UTF-16 plus requested/local-codepage fallback with replacement-safe terminal output | A; encoding contract tests |
| Daemon lifecycle | signals and POSIX permission bits | High | Windows PID identity plus protected state/token DACL and authenticated lifecycle | A source; W10 native ACL/daemon owner pending |
| Auth token | `chmod(0600)` is meaningful only on POSIX | High | Protected current-user-and-SYSTEM DACL; actual path inspection in Doctor; auth remains mandatory | A when verified, otherwise explicit B |
| Clipboard | pbcopy/wl-copy assumptions | Medium | Existing clipboard adapter selects `clip`/PowerShell on Windows and reports helper availability | A when helper present, B/C otherwise; W12 |
| External editor | Unix editor names and `shlex` semantics | Medium | Product probe resolves configured editor and Windows terminal parsing already uses `shlex(..., posix=False)` | A when configured; W2/U1 |
| LSP | executable lookup may require `.exe`/`.cmd` wrappers | Medium | `shutil.which` is the boundary; Windows PATHEXT performs wrapper resolution | A when server installed, B optional; W14 |
| MCP stdio | child transport and termination can inherit POSIX assumptions | High | MCP already uses argv rather than a shell; native smoke and process cleanup remain explicit Windows acceptance gates | A transport contract; W15 host evidence pending |
| Tree-sitter | binary wheel availability | Medium | Conditional capability probe and AST/structural-search fallback | A when wheel imports, B otherwise |
| Terminal width | Unicode cell width and narrow layouts | Medium | Presentation tokens and logical frame tests own narrow/CJK behavior in the UX tasks | Pending U12/U14 in this phase |
| Evaluation helpers | several developer-only runners use POSIX shell quoting/signals | Medium | They are not imported by the installed product startup; Windows release workflow invokes only platform-aware product scenarios | B developer tooling; not a runtime blocker |
| chmod/flock stores | provider, preference, Session, attachment, history files | Medium | shared DACL adapter protects state roots/final files; Provider credential temp is protected before secret write | A source; native ACL round-trip pending |

## Source scan disposition

- Runtime-critical POSIX PTY and shell assumptions have been moved behind
  `platform_runtime.py` and `process_backends.py`.
- Product modules with explicit platform branches include daemon, clipboard,
  terminal input, MCP, provider storage, and process service. They remain in
  the Windows W1–W15 acceptance manifest instead of being inferred from Linux.
- POSIX-only evaluation adapters are not used as evidence for Windows support.
- A Windows release claim requires a successful `windows-latest` run; Linux
  dependency injection proves selection logic only.
