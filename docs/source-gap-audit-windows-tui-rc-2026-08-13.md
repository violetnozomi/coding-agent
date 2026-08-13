# Source gap audit: Windows + TUI RC closure

The closure scan searched production Python for `TODO`, `FIXME`,
`NotImplemented`, bare `pass`, `restart_required`, `unsupported`, and
`unavailable`. It found 350 textual matches. Every match was assigned to one of
the classes below; this is not a claim that every future enhancement was built.

| Classification | Disposition | Representative owners |
|---|---|---|
| Real RC bug | Fixed | stale R5/R11 acceptance paths, Remote `Path` shadowing, UTF-8-only subprocess decode, missing Job kill-on-close, ConPTY without Job ownership, lexical-only Windows path checks |
| Language/control-flow construct | Retained | exception marker classes, `except` cleanup branches, optional callback failures, prompt-toolkit `NotImplemented` event propagation |
| Honest platform/capability limitation | Retained and surfaced | Windows ACL is Tier A only after adapter/path verification and otherwise Tier B; optional LSP/MCP/provider availability, true remote file upload, raw nested terminal attach remain explicit |
| Extension lifecycle truth | Retained | Skill hot enable/disable; MCP/tool-pack changes that really need restart continue to report `restart_required` |
| Abstract/test/fixture text | Retained | benchmark abstract methods, generated sample TODO text, pass/fail prose, deterministic failure fixtures |
| Future enhancement outside Core Freeze | Deferred | marketplace/cloud, semantic productionization, raw xterm emulator, macOS native matrix |

No production `TODO` or `FIXME` was found that represents an unfinished item in
this RC prompt. The one `TODO` string is generated benchmark fixture content.
`NotImplemented` in `fullscreen.py` is the prompt-toolkit mouse-handler sentinel,
not an unimplemented product screen. Bare `pass` sites are either empty marker
exceptions or best-effort cleanup/fallback paths; executable acceptance covers
their owning lifecycle.

The two deliberately incomplete product contracts are explicit:

- Cross-host Remote does not upload client-local files. The UI disables those
  attachments and identifies the endpoint.
- Raw PTY/ConPTY screen takeover is not merged. Stable read/write/resize/log
  controls remain the supported interactive-process contract.
