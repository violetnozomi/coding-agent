# Offline timeout failure-domain investigation

Baseline: 4e59fe75032dd32186b73d92b7ad98c955866176 (production 45db8e3).
Initial HEAD/origin/main match, worktree clean. 0 paid model requests.

1. Read historical Q evidence and production stream/tool/session/recovery chain.
2. Add NativeSDKRunner integration with a scripted streaming Provider, real Bash
   timeout (1 second, the supported minimum), genuine hooks and checkpointing.
   Reject socket network access. Capture exception chains before sanitization.
3. Assert a second model turn sees the canonical failed tool message. If GREEN,
   restore additional historical conditions individually; do not guess a patch.
4. Only after a deterministic RED and exact stack, fix the smallest owning boundary.
5. Exercise timeout/nonzero/denial/success, infrastructure error, Provider error,
   cancellation and verification generation; run related Core/security regressions.
6. Preserve historical evidence hashes, save RED/GREEN logs and source analysis,
   lint/audit, commit and push only justified changes.
