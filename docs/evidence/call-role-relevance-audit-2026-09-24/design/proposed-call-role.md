# Local structural contract (proposal only)

Attach one role at RawCallRecord emission from the concrete Call AST object:
Return.value is call => returned; Yield.value is call => yielded;
Expr.value is call => discarded; Assign/AnnAssign/NamedExpr.value is call => assigned;
Call.args contains call or keyword.value is call => argument;
If/While/IfExp/Assert.test is call => condition; otherwise unknown.

Do not propagate ancestor Return through other Calls, BoolOp, Await, containers,
comprehensions or variable names. YieldFrom is distinct from Yield and remains
unknown in this proposal. Starred argument wrappers likewise remain unknown.
A direct Boolean operand can return a value rather than act as a predicate, so
BoolOp does not automatically mean condition. Unknown is intentional, not failure.

Generate inside existing AST visitor using traversal parent/context. No source
regex, extra parse, type inference or dataflow. Append metadata for all emitted
calls, including unresolved ones; do not extend existing call discovery scope here.
