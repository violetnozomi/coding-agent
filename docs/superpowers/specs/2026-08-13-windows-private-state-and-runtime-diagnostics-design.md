# Windows Private State and Runtime Diagnostics Design

## Scope

This continuation stays inside the Windows RC freeze. It does not change the
Agent, Session, tool, repository-intelligence, or ProcessService public
contracts. It closes three host-specific gaps left by the previous RC audit:

1. sensitive local files relied on POSIX mode bits that are not Windows DACLs;
2. process decoding knew only the configured and locale encodings, not the
   native Windows ANSI/OEM code pages or UTF-16 output without a BOM;
3. native acceptance proved Job Object cleanup but did not expose the selected
   lifecycle mode in backend diagnostics.

## Design

`nz_coder.private_paths` owns one small cross-layer platform contract. On
POSIX it applies mode `0700` to directories and `0600` to files. On Windows it
uses `ctypes` and the documented security APIs to replace inheritance with a
protected DACL granting full control only to the current user and Local System.
The low-level API is loaded lazily so importing NZ-Coder remains portable. A
result object reports whether the strong ACL was applied; callers that already
have authentication may continue with an honest Tier B fallback when the host
rejects DACL changes.

The contract is applied at the security-critical write boundaries: daemon
directory/token/state/lock/log, workspace provider `.env`, initializer `.env`,
clipboard image cache, prompt history, and terminal preferences. Atomic
replacement hardens the final path after replacement so a permissive existing
file cannot survive an update.

`decode_process_output` keeps the existing order—BOM, UTF-8, configured
encoding, native system encodings, replacement—but native Windows system
encodings now include `GetACP()` and `GetOEMCP()`. A conservative NUL-layout
heuristic recognizes UTF-16LE/BE without a BOM before trying UTF-8.

Process backends expose a read-only `lifecycle_mode` string. It is
`windows-job-object` only when binding succeeded, otherwise
`windows-taskkill-fallback`; POSIX uses `posix-process-group`. This does not
change ProcessHandle serialization or ProcessService ownership.

## Error handling and trust

- ACL helpers never claim Tier A from `chmod` on Windows.
- A Windows API error is returned as bounded diagnostic data without secrets.
- The daemon and provider setup keep their existing availability behavior and
  remain authenticated even when ACL application is unavailable.
- No process-name scan is introduced; Job Objects still own only children
  created by ProcessService.

## Verification

Unit tests inject the OS/API boundary and exercise real file side effects.
Native Windows tests apply and inspect a real DACL, verify daemon token
hardening, decode code-page output, and require Job Object lifecycle mode.
Linux runs all platform-neutral tests and skips only proofs requiring the
Windows kernel. The final gate includes focused tests, the full suite, Ruff,
compileall, and the existing native Windows workflow configuration.
