# Source envelope contract

AST_NATIVE Python owns ClassDef.lineno/end_lineno and decorator expression lineno/end_lineno/col_offset during the original analyze_file parse. Current SymbolRecord/DB only preserve class line/end_line. Future preserve additive nullable source_start_line (generic source-boundary fact), existing end_line, and documented provenance/completeness. Generate at original AST emission; transport through SymbolEntry/DB/query metadata; rebuild disposable cache after schema bump. Do not reconstruct in Sidecar.

Normal single-line, multiline call-form and stacked decorators: source_start_line=min(class.lineno, decorator linenos). End is class.end_lineno. This includes declaration, inheritance syntax, whole body, methods, __init__, __post_init__ and class-body validators; nested class excludes Outer. It does not include imported aliases, base implementations or decorator execution semantics. Never claim complete runtime constructor semantics from local text alone.

Additional boundary discovered here: @(
 decorator
) has decorator expression lineno on the inner expression, not the @ line. Naive min(lineno) loses syntax. Future first implementation must fail-soft omit when complete start cannot be certified. At the original AST coordinate, validate that the expression prefix is indentation followed by @; this is an exact-coordinate check, not backward scanning or regex span inference. Store no certified start if it fails. No source scan tries to repair an unsupported form. Multiple decorators must ALL pass, even if an earlier start looks safe.

T1 character truncation is unsafe: a late __post_init__ disappears from the first 1400 characters. T2 whole-envelope include-or-omit is recommended. An oversized or uncertified envelope emits no class source; bounded trace reason envelope_exceeds_budget/decorator_start_unproven. T3 selective methods is deferred: indirect validators/alias/base/decorator semantics cannot be reduced to a small method-name list without more proof.

Classification METADATA-ENRICHMENT-REQUIRED: AST has sufficient original information for the tested supported subset, but production index does not retain it. Parenthesized unusual forms may remain safely unsupported. Source envelopes in this directory are offline observations only; no production metadata or Sidecar packet changed.
