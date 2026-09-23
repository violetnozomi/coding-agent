# Candidate routes

**Route A** is the viable route in this audit: use existing high-confidence
changed-symbol callee edges, select unchanged class definitions, and render a
bounded class envelope. On C, the existing index resolves parser `loads` to
`Config` and also resolves writer/save relations. It does not mean every
class callee is relevant; selection still needs directness, confidence,
unchanged status, a source-language implementation file, and a small budget.

**Route B** is only partially supported. The graph exposes `store.save →
writer.dumps` and parser `loads → Config`, but it does not provide a reliable
type/dataflow edge connecting an arbitrary serialization argument to its class
definition. No field-name or package-name heuristic was added.

**Route C** is unsupported by the current host. The authority text is retained
as text, but there is no generic exact symbol-reference resolver for prose.
Adding one would introduce a second natural-language selector and is outside
this design.

