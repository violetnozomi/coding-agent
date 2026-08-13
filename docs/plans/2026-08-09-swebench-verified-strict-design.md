# SWE-bench Verified Strict Leaderboard Design

## Goal

Make SWE-bench Verified (500 test instances) the only leaderboard-grade NZ-Coder run profile. A run is strict pass@1: each instance receives exactly one inference attempt, the prompt contains only public issue data, answer-searching tools are unavailable, and the result can be packaged with inference-time trajectories and official evaluation logs. SWE-bench Lite (300 instances) remains a development smoke profile and is never presented as the main score.

## Profiles

- `verified`: `princeton-nlp/SWE-bench_Verified`, `test`, expected cardinality 500, leaderboard eligible only in strict mode.
- `lite`: `princeton-nlp/SWE-bench_Lite`, `test`, expected cardinality 300, development smoke only.

The dataset is selected by a named profile, never by a free-form default hidden in the adapter. Manifests record the profile, dataset, split, selected IDs, and whether a partial selection was used.

## Strict inference contract

Strict mode:

1. Builds the user prompt from `instance_id`, repository identity, base commit, and `problem_statement` only. It never reads or includes `hints_text`, `FAIL_TO_PASS`, `PASS_TO_PASS`, official logs, or gold patches.
2. Allows one Agent invocation per instance. Empty patches remain empty predictions; they are not retried.
3. Disables `webfetch`, remote search, MCP tools, child-agent spawning, and other indirect extension paths. Bash remains available for local repository inspection but rejects network-client and remote-repository commands.
4. Writes an append-only, exact-once attempt journal. Resume skips completed instance IDs and never selects a later attempt based on evaluation outcomes.
5. Marks partial selections, policy violations, missing trajectories, duplicate IDs, or missing official logs as submission-ineligible.

The journal writes a durable `claim` before setup or inference and a separate
`result` afterward. A claimed instance is never started again, including after
a crash. Strict worktrees are rebuilt as one-commit base snapshots so commits
and refs created after the benchmark base commit cannot reveal the gold fix.

`retry-agent` remains available only as an explicitly diagnostic workflow. Its artifacts are marked `leaderboard_eligible: false` and cannot be accepted by the strict submission packager.

## Evidence and submission bundle

Each inference produces a sanitized JSONL trajectory at inference time. Public trajectories retain prompts, model/tool lifecycle, tool names, bounded inputs/outputs, and timing while removing credential-like values and local absolute path prefixes.

The packager creates:

```text
evaluation/verified/<run-name>/
  all_preds.jsonl
  metadata.yaml
  README.md
  manifest.json
  trajs/<instance_id>.jsonl
  logs/<instance_id>/patch.diff
  logs/<instance_id>/report.json
  logs/<instance_id>/test_output.txt
```

Before packaging it checks the official profile instance-set digest, unique
predictions, schema, one durable claim and result per instance, strict/no-leak
manifest fields, trajectory prompt/request evidence, trace tool policy, source
and artifact hashes, and required official log files. Failed checks produce a
report but no eligible bundle.

## Model default

The product default is changed to the OpenAI-compatible model ID `deepseek-v4-flash` and the DeepSeek API endpoint. Environment variables continue to override all defaults so a provider's exact deployed model alias can be supplied without code changes.

## Compatibility

Legacy `run-agent`, `run-eval`, and `retry-agent` commands remain. `run-agent` defaults to the Verified strict profile; Lite must be requested explicitly. Existing retry logic is preserved for diagnostics but separated from leaderboard claims.
