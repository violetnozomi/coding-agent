# Plan

1. Freeze baseline and source hashes.
2. Write production-facing role tests before editing production; preserve harness errors separately.
3. Extract local roles inside the existing AST visitor; persist additive metadata through SQLite and API edges.
4. Verify schema rebuild, warm reuse, incremental cache invalidation and resolver preservation.
5. Replay frozen C in a disposable workspace; do not build Sidecar constructor evidence.
6. Run focused and broad offline regressions, freeze integrity evidence and commit.
