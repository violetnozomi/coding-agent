# Usage-role provenance options

P-A (recommended for current built-in v4 pipeline): join edge.caller_symbol_id and edge.path to caller SymbolEntry and FileEntry in the SAME fresh snapshot. Require Python, AST_NATIVE/python-ast on file and owner, no parse_error, current fingerprint, role != unknown. Require ready/indexed consistent service generation when collecting. This is a pipeline invariant, not a conclusion from target confidence. _replace persists file/symbol/call output together; Python emission owns local role; non-Python emitters retain unknown. LSP changes target fields only.

P-B: explicit usage_role_source/capability would clarify standalone edge portability and future mixed analyzer producers. Not required for the current closed built-in pipeline with caller join. Recommend adding only if independent role producers, imported edges, mixed per-file role origins or standalone consumers remove that invariant. Would require persistence/rebuild/API tests; not implemented here.

P-C: edge.source is not sufficient. A real augment_call_targets fake-LSP fixture changes python-ast to lsp-definition while preserving returned and unknown roles. Target provenance and local role provenance are separate dimensions. Do not reject legitimate returned solely because target was LSP-resolved or promote unknown because LSP succeeded.

Caller capability ALONE as an unjoined string is insufficient. Classification CALLER-CAPABILITY-SUFFICIENT means the full current same-snapshot pipeline contract above. Corrupted caches or arbitrary plugins forging role metadata are not authenticated by this inference. Unknown stays unsupported. Prototype explicitly rejects non-Python, lexical capability, parse_error, wrong source and stale caller.
