# 1. Classification

**CF-C2: ACCEPT.** One real semantic-verifier request received `accept` after the
current production bounded unchanged dependency section was added. The known-bad C
implementation was not changed.

# 2. Baseline and authorization

Baseline was `ce93e9c4f97dd6492e5f6fcdf9d54ecd1b8bc5ec`; HEAD, origin/main and the
initial worktree were equal/clean. The current user authorized exactly one paid
semantic-verifier physical request, with zero Main, planner, embedding and InfCodeX
requests, and no retry. A local preflight capture mistake occurred before network
I/O and is recorded in `analysis/harness-preflight.json`; it consumed no request.

# 3. Historical reviewer

The baseline is the historical corrected-diff CF-C request using `deepseek-v4-flash`,
the same system prompt, forced `emit_sidecar_verdict` tool, `max_tokens=1024`,
thinking disabled, genuine task, retained CONFIG_SPEC, recent transcript, changed
diff, exact current-generation `46 passed` output, final assistant text and frozen
additional criteria. Historical verdict: **accept**.

# 4. Controlled delta

CF-C2 used that serialized request and inserted exactly the current production
collector's `supporting_repository_evidence` section immediately before the Main
final text. The section is not manually authored. It contains exactly three
high-confidence direct callers in 889 characters:

- `configkit/config/migrate.py:migrate_file`
- `configkit/store.py:load`
- `configkit/store.py:save`

The `store.save` span is current unchanged workspace source:

```python
def save(path, config):
    encoded = dumps(config)
    config_path(path).write_text(encoded)
```

Digest: `86cce2b6ed2a5330284d72da74baad6132bab6e336cd4f93ac8969483d34f5d4`.

# 5. Single-variable audit

Passed. System message, model, tool schema, thinking setting, max tokens, transcript,
authority, changed-file diff evidence, verification evidence, final answer and
criteria were equal. Request hashes are in `counterfactual/request-diff.json`.
The only rendered delta is the bounded dependency section. Packet audits confirm
the original no-overwrite and bool-exclusion clauses, writer diff and `46 passed`
remain visible. `11/12`, the hidden failed-check name, evaluator and oracle content
are absent.

# 6. Frozen implementation facts

The frozen workspace remains project tests **46 passed** and independent acceptance
**11/12**. The known missing behavior is direct construction of an invalid Config
being serialized and written through `writer.dumps`/`store.save`, allowing an
existing destination to be overwritten. No business file, CONFIG_SPEC or test was
modified. Evaluator truth was never placed in the request.

# 7. Real reviewer result

The sole request returned HTTP 200, `finish_reason=tool_calls`, one valid
`emit_sidecar_verdict` call, and `verifier_ok`:

```text
verdict: accept
suggestedFix: ""
```

The reviewer reason praised v2 serialization, parser validation, CLI dry-run,
source preservation, compatibility and the trusted 46-pass command. It claimed
the invalid-config no-write guarantee was structurally enforced because the
migration path validates before save. It did not identify the direct `Config`
construction path, writer boundary validation gap, `store.save` overwrite path or
destination-preservation defect.

# 8. Result meaning

This is **CF-C2: ACCEPT**. In this one frozen sample, corrected changed-file
attribution plus full authority, exact verification and current bounded unchanged
`store.save` evidence did not change the reviewer from accept. This does not identify
whether the remaining limitation is evidence topology beyond selected direct callers,
obligation granularity, or reviewer semantic judgment.

# 9. Accounting

Physical requests: **1**. Main: **0**. Planner: **0**. Embedding: **0**. InfCodeX:
**0**. Retries: **0**. Provider/model: openai-compatible / deepseek-v4-flash.
Usage: prompt **12,015**, completion **476**, total **12,491**, cached **7,296**;
duration **2.5415914559853263 seconds**. Cost: **unknown**; billing was not exposed.

# 10. Integrity

`verify.py` checks production hashes, no production diff, historical evidence
immutability, packet privacy and the one-request accounting. The frozen C workspace
hashes remain the previously captured values. No hidden evaluator data or private
reasoning fields are present in the published result. `SHA256SUMS.json` covers this
directory.

# 11. What this proves

The current bounded dependency channel can deliver `store.save` to a real semantic
reviewer packet, and that packet was accepted by the reviewer in this sample.

# 12. What this does not prove

It does not prove reviewer reliability, a success rate, a Main Agent improvement,
that dependency evidence was the historical false-accept's unique cause, that
TaskContract/Ledger are sufficient, that adding model.py would change the verdict,
or that any general semantic coverage issue has a unique root cause. No second
request, C rerun, A/B task or Core patch was performed.

# 13. Next action

Do not patch Core from this result. Perform a separate offline evidence-sufficiency
and semantic-proof audit before proposing another experiment or any production
change. This one-request authorization is consumed.
