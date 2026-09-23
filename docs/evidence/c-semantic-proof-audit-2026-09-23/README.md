# C semantic proof audit

Classification: **PROOF-AUDIT: MIXED**.

The frozen C implementation has a deterministic destination-preservation
defect. The CF-C2 packet contains the authority, changed writer evidence, and
`store.save`, but it does not contain the constructor evidence needed to prove
that an invalid `Config` can enter the write boundary without passing through
`load()`. The reviewer’s argument is locally valid for `migrate_file`, then
unsupportedly generalizes that fact to every write surface.

## Frozen facts

The audit ran from commit `949f867892ab4dad56f5914ecad4276be5e99757` with a
clean tree. It made zero provider requests and changed no production source,
the frozen C workspace, or prior evidence. The historical C result remains
46 project tests passed, 11/12 independent acceptance, and Runtime completed.

## Proof graph

`CONFIG_SPEC.md` explicitly requires that no validation error overwrite the
input or an existing destination, and requires `port` to be an integer in
1..65535 excluding `bool`. It also says existing `Config` construction works.

The frozen model is a dataclass with no custom `__init__` or `__post_init__`.
`Config("demo", "localhost", False)` therefore constructs successfully.
`writer.to_dict` and `writer.dumps` serialize its fields without validation.
`store.save` obtains `encoded = dumps(config)` and writes it with
`write_text`. In a disposable workspace this changes an existing destination
from `{"existing":true}` to JSON containing `"port": false`.

The migration path is separately safe for the tested malformed input:
`migrate_file` loads and validates before attempting the write, raises
`ConfigError`, and preserves the input bytes. That local fact does not prove
the direct `save(path, Config(...))` surface safe.

## Premise coverage

The minimum proof premises are recorded in
`proof/premise-table.json`. P1 (authority) and P4 (`store.save` writes the
serialized value) are explicit in the packet. P3 (writer serialization) is
inferable from the writer diff. P2 (an invalid object can exist independently
of parser validation) is absent: CF-C2 has no `Config` definition or equivalent
constructor witness. P5 (direct save is an existing compatible surface) is
ambiguous. Consequently V0 is not proof-complete.

V1 is an offline synthetic audit variant that adds only the current
`Config` source span. It is a premise-coverage aid, not a production packet
and was not sent to a reviewer. It does not predict a model verdict.

## Repository intelligence boundary

The existing structural graph exposes parser-to-`Config`, writer-to-`save`,
and `save`-to-`dumps` relations, including the direct caller
`configkit/store.py:save`. It does not expose a type/precondition/dataflow
fact that a caller may independently construct an invalid `Config`. This is a
missing constructor/precondition semantic relation, not evidence that the
graph or sidecar should be changed in this audit.

## Root-cause boundary

The result is mixed: the packet has an evidence-topology gap, and the final
review reasoning makes an unsupported positive inference beyond the packet.
This audit does not establish which factor would determine a future reviewer
verdict, does not justify adding `model.py` to production collection, and does
not justify changing the TaskContract, RequirementLedger, prompt, or
completion gate. No hidden evaluator fact was used to build the production
packet; the counterexample is evaluator-side offline proof only.

The next useful step is a separately authorized design discussion for a
bounded, generic constructor/precondition evidence invariant. No Core change
is recommended from this audit alone.
