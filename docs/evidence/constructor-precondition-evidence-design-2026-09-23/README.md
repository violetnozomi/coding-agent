# Constructor/precondition evidence design audit

Classification: **PRECOND-DESIGN: EXISTING-GRAPH-SUFFICIENT** for a bounded
common case, with an explicit partial boundary. The current structural index
already locates the C `Config` class through the parser-to-class relation and
locates the changed writer's callers. The missing capability is selection and
projection of a class source envelope; a new type/dataflow graph is not needed
for this design. The index does not prove arbitrary argument types or invalid
state, so those cases remain fail-soft and evidence is not proof by execution.

## Results

Route A is usable when an existing high-confidence structural class relation
is available. On C, `parser.loads → Config` is present and `Config` is an
unchanged Python class. The changed writer's direct graph edges still locate
`store.save` and `migrate_file`; the graph does not claim that `writer.dumps`
has a statically proven `Config` parameter. Route B has no general type or
dataflow relation, and Route C has no safe authority-text symbol resolver.

The recommended source envelope is E1: contiguous decorators immediately
preceding a class declaration plus its class body. Plain body-only E0 omits
`@dataclass(frozen=True)`. E1 preserves decorators, inheritance, explicit
`__init__`, `__post_init__`, and class validators. Imports are not included by
default. The generic fixtures confirm decorator retention, validation-method
retention, deterministic two-symbol selection, test-source exclusion, and
fail-soft unresolved handling.

The candidate budget is at most 1–2 class symbols, 1,400 characters each,
2,400 total, deterministic relation/confidence/path order, with truncation
markers. Truncation before a constructor or validator makes the evidence
insufficient for a constructor proof. Source is current, bounded, confined,
non-executed, and explicitly non-authoritative.

## C and P2

The existing graph exposes `parser.loads → Config`, `writer.dumps → store.save`
and the changed writer callers. It does not expose constructor preconditions or
invalid-state dataflow. E1 therefore supplies the missing source premise for
an offline V1 design packet: the decorator and class body show that the
dataclass has generated construction with no visible validation hook. This
does not execute the constructor and does not predict a reviewer verdict.

## Boundary

This audit does not add a collector, graph relation, prompt text, TaskContract,
Ledger rule, or production code. It proves that a generic bounded
selection/projection channel can reuse existing structural intelligence for
the common class-relation case. It does not prove that the graph can locate
all models, infer arbitrary types, establish that every object is invalid, or
make semantic review complete. A future implementation should begin with
Route A plus E1 and omit unresolved, low-confidence, test-only, stale, or
out-of-budget candidates.

