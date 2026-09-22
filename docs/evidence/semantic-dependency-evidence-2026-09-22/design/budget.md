# Fixed independent budgets

- Up to 32 changed paths; larger change sets gracefully omit this channel.
- changed_scope: limit 16, node_limit 20, max_depth 1, time_budget_ms 50,
  confidence_threshold 0.85, wait_budget_ms 0.
- Complete scope/snapshot/current-source collection uses the existing service worker
  with a caller-owned 500 ms deadline. No index warmup, LSP or embeddings.
- Up to 3 symbols; each rendered block <= 1,200 characters; complete section <=
  4,000 characters, including labels and omission count.
- Source files <= 128,000 bytes, UTF-8 without NUL. Use symbol line/end_line;
  long spans retain a counted truncation marker, not entire file contents.
- Omitted count counts the bounded returned candidate set, not unseen repository
  dependencies. Scope truncation is separately traced. Count includes policy/safety
  exclusions. Lexical path/symbol ordering is stable and deliberately not C-tuned.

The internal graph time budget does not bound its complete database work. The
outer Future deadline is essential. A timed-out running query cannot be forcibly
cancelled; it may finish on the existing workspace worker, but its result is discarded
and cannot delay the current review beyond the wait deadline or trigger a model.
The helper does not create a worker or wait for it to shut down.
