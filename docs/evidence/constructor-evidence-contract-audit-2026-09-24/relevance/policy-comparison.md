# Policy comparison

These are engineering policy judgments supported by deterministic bounds, not measured reviewer precision/recall.

| Criterion | A: strict semantic trigger | B: bounded structural context |
|---|---|---|
| Precision | Desired semantic relevance cannot currently be established | Returned-target identity is precise; semantic relevance explicitly unclaimed |
| Recall | Unknown without new semantic capability | Misses assigned values, inner arguments, unresolved calls and oversized classes |
| Determinism | No existing general serialization/persistence classifier | Active review + current changed caller + trusted returned edge + resolved unchanged class |
| Complexity | Would require independently justified semantic rules/type/dataflow/task interpretation | Existing identity/capability/role plus bounded source projection |
| Cross-project | Risks name/task-specific heuristics | No names, field shapes, task prose, or package layout |
| Security | Still needs source confinement | Existing safe source/WFA; text is not instructions |
| Explosion | Unknown until trigger exists | At most two complete blocks / 2400 rendered characters |
| False positives | Goal of zero not established as necessary | Telemetry is accepted as optional structural context; worst case both slots irrelevant |
| False negatives | Potentially high and unquantified | Explicit conservative omission is normal |
| Extensibility | Separate capability project | Extend supported roles only after independent evidence |

Choose B. Existing dependency_evidence.py already uses high-confidence direct structural callers, a strict symbol budget and non-authority label; it does not prove task relevance. The same philosophy permits a small returned-class context budget. This does NOT prove harmlessness to every reviewer: distraction/displacement remain possible, including a lexically earlier irrelevant class taking a slot. No numerical precision rate or reliability gain is claimed.

Semantic review active is a lifecycle gate, not a semantic classifier. changed_scope risk, public API exposure and deletion/integrity signals are not promoted into a serialization detector. No such detector is needed under B's narrower claim.
