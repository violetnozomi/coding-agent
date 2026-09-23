# C semantic proof audit plan

This is a provider-free audit of the frozen CF-C2 packet. It reconstructs the
authority, object-construction, serialization, persistence, and migration
premises for the known destination-overwrite defect. It then compares the
actual packet (V0) with an evaluator-only synthetic packet (V1) that adds only
the missing `Config` source span. No packet is sent to a model.

The audit also replays the existing structural repository graph to record
which relations it exposes and which constructor/precondition relations it
does not expose. It does not change the graph, collector, prompt, contract,
ledger, or completion policy.
