# Current source and confinement

Graph output locates a candidate; it cannot authorize file access. Lexical relative
source paths reject absolute, traversal, backslash/drive forms, hidden components,
evaluator/oracle/acceptance, tests/docs/logs/generated/vendor directories and test
filenames. Retained authoritative reference paths and all changed/deleted paths
are excluded. Workspace identity must match the service's root.

Reads use existing handle-anchored WorkspaceFileAccess with file size limits and
no symlink following. Index snapshot fingerprints must match current source
mtime/size; source hash comes from the same handle as actual bytes. Stale changed
roots cause omission; stale candidate spans are skipped. Definitions must be
unique and high-confidence; no second AST or regex call parser is introduced.

Deleted changed roots may use an existing indexed impact relation, but source is
only read from still-present unchanged dependencies. After graph refresh removes
the deleted symbol, evidence can naturally disappear; no relation is invented.

ready/indexed means the architecture's structural confidence, not a formal proof
of all dynamic calls. Fingerprint freshness inherits the existing index contract
(mtime/size); adversarial edits preserving both can defeat it. Current-source hashes
still invalidate accepted evidence when bytes differ. This patch does not claim
cryptographic provenance for graph edges. No source text is copied into trace.
