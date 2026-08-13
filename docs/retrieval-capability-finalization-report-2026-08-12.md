# Retrieval Capability Finalization

Date: 2026-08-12

This report separates feature coverage, implementation depth, and measured
behavior. It does not convert structural probes into a general capability
score. InfCodeX and OpenCode were not run under the same model, fixture, and
budget, so reference behavioral parity remains unavailable.

## Outcome

- Retrieval routing now keeps task-classification confidence separate from
  accepted repository evidence. Low-confidence or ambiguous candidates are not
  injected merely because the task classifier is confident.
- Short business-intent prompts are covered by a 35-query routing confusion
  suite. Exact files, symbols, literals, changed scope, structural questions,
  business intent, and simple tasks retain separate routes.
- Semantic tool exposure is capability-aware. An unconfigured or failed
  provider does not add `semantic_search` schema tokens or selection noise.
- Python is running the AST-native tier; TypeScript, JavaScript, and Go are
  running tree-sitter in this installation. CI also verifies honest lexical
  fallback without parser wheels.
- A production benchmark exposed one LSP process leaked per SDK run. LSP
  clients now close on the final workspace lease while other workspaces remain
  connected. The resumed real-model run held zero or one LSP process rather
  than accumulating 88 processes.
- Semantic retrieval remains **optional experimental**. It has measured
  localization-efficiency value, but its current full re-embedding and
  in-memory store are not production index depth.

## Implementation Changes

`RepoRoutingSignal` now reports `routing_confidence`,
`evidence_confidence`, `candidate_count`, and `fallback_state` independently.
Auto-context gates on accepted evidence, not on the route classifier. Semantic
cosine values are calibrated with rank separation because absolute scores are
not comparable across embedding models. Only the leading distinct locator can
be injected, and a close runner-up is rejected.

`SentenceTransformerEmbeddingProvider` now has observable `configured`,
`loading`, `ready`, `unavailable`, and `failed` states. A configured provider is
prepared before the run exposes its tool. The doctor probe is independent of
model-provider initialization.

Behavioral evaluation now includes I2/I3/I4 and the short IS vocabulary
mismatch case by default. The CLI supports semantic-only controls and strict
resume matching, so an interrupted 108-run matrix reused 95 exact reports and
executed only the missing 13.

## Parser Runtime

The actual doctor result is:

| Language/runtime | Active tier | Status |
|---|---|---|
| Python | `ast-native` | available |
| TypeScript | `tree-sitter` | available |
| JavaScript | `tree-sitter` | available |
| Go | `tree-sitter` | available |
| Watcher | `watchfiles` | available |
| LSP augmentation | installed server probe | available |
| Semantic retrieval | provider unconfigured by default | experimental |

The parser wheels and `watchfiles` are declared in `pyproject.toml`.
`.github/workflows/repo-intelligence.yml` installs and executes the TS alias,
namespace call, JS import/require, and Go package-call contracts. A separate CI
job uninstalls parser wheels and asserts `lexical-fallback` honestly.

## Retrieval Policy A/B

Evidence: [structural production matrix](../.nz-coder/benchmarks/retrieval-finalization-20260812-structural/production-retrieval-matrix.json).
All 108 runs used `openai-compatible/deepseek-v4-flash`, provider-default
reasoning, temperature 0, max 20 turns, and three repetitions.

| Strategy | Runs | Success | Mean localization turn | Mean reads | Mean wrong reads | Mean turns |
|---|---:|---:|---:|---:|---:|---:|
| tool-only | 27 | 81.5% | 1.11 | 4.70 | 1.04 | 9.74 |
| guidance | 27 | 77.8% | 1.26 | 4.37 | 0.85 | 9.67 |
| auto-context | 27 | 74.1% | 1.07 | 4.63 | 0.93 | 9.56 |
| policy | 27 | 77.8% | 1.22 | 4.41 | 0.89 | 9.33 |

Case B failed under every strategy because the agent reached the same
verification-policy stop after making the rename. It is not evidence of a
retrieval difference. Excluding B and the deliberately hard short IS gate,
tool-only, guidance, and policy were 21/21 successful; auto-context was 20/21.
Policy used the fewest turns (6.62), but no strategy had a decisive task-success
advantage.

The default therefore remains **guidance**. The evidence does not justify
changing it globally to policy or unconditional auto-context.

## Semantic A/B

Evidence: [semantic production matrix](../.nz-coder/benchmarks/retrieval-finalization-20260812-semantic/production-retrieval-matrix.json),
[calibrated IS matrix](../.nz-coder/benchmarks/retrieval-finalization-20260812-semantic-is-calibrated/production-retrieval-matrix.json), and
[direct ranking quality](../.nz-coder/benchmarks/retrieval-finalization-20260812-semantic-quality-v2.json).

The 42-run semantic matrix used the same DeepSeek conditions and local
`sentence-transformers/all-MiniLM-L6-v2`. On A/D/I/I2/I3/I4, policy results were:

| Retrieval | Runs | Success | Localization turn | Reads | Wrong reads | Turns | Wall time |
|---|---:|---:|---:|---:|---:|---:|---:|
| structural policy | 18 | 100% | 1.28 | 4.00 | 1.22 | 7.00 | 22.0s |
| embedding policy | 18 | 100% | 1.00 | 3.33 | 0.67 | 6.17 | 19.5s |

Embedding improved efficiency but not task success. Direct retrieval placed a
correct file at rank 1 for I/I2/I3/I4/IS and achieved 100% expected-file recall
at 10. Correct-vs-best-wrong score margins were 0.156, 0.167, 0.056, 0.241,
and 0.021 respectively. This evidence led to the relative confidence gate; the
previous fixed 0.35 cosine threshold rejected every IS result even though the
correct file ranked first.

After calibration, the cold IS probe found accepted evidence in 3/3 runs at
100ms, 250ms, and 500ms, with median elapsed time around 72-75ms. The 50ms tier
remained 0/3. The production default stays at 100ms.

The calibrated real-model IS comparison was:

| Strategy | Success | Reads | Precision | Turns | Wall time |
|---|---:|---:|---:|---:|---:|
| semantic tool-only | 3/3 | 5.67 | 0.767 | 8.67 | 35.0s |
| semantic policy | 3/3 | 4.00 | 1.000 | 5.33 | 19.1s |

The earlier uncalibrated IS matrix was 0/3 for both groups. Because tool-only
itself varied from 0/3 to 3/3 across repetitions, this is strong efficiency
evidence but not stable proof of a success-rate gain.

## Semantic Scalability

Evidence: [semantic scalability](../.nz-coder/benchmarks/retrieval-finalization-20260812-semantic-scalability.json).

| Files | Initial chunks embedded | Chunks after one-file edit | Initial query | Update query |
|---:|---:|---:|---:|---:|
| 100 | 100 | 100 | 8.3ms | 7.5ms |
| 500 | 500 | 500 | 37.1ms | 40.1ms |
| 2,000 | 2,000 | 2,000 | 207.0ms | 202.8ms |

A one-file edit re-embeds every chunk. The chunk coverage probe also found that
symbol chunks include the function but omit the module-level policy constant.
There is no stable chunk id/content-hash delta, persistent vector storage, or
incremental embedding cache yet.

**Semantic decision: retain as optional experimental.** Do not make it a core
dependency and do not start a vector-database project. If a later phase chooses
productionization, it must first add stable chunk ids, content hashes,
changed/new/deleted chunk diffs, embedding cache keys, module/top-level chunks,
and a lightweight persistent local store.

## Three-Way Source Matrix

| Capability | nzcoder | InfCodeX | OpenCode/Kilo | Verdict |
|---|---|---|---|---|
| Retrieval policy | Deterministic route, split route/evidence confidence, bounded guidance/auto-context | Rich preturn routing middleware and bundles | Prompt guidance plus search tools | Mostly aligned; different composition |
| Structural lookup | Unified symbol/module/process intent lookup | `semantic_lookup` is structural symbol/module/process ranking | Structural tools plus codebase search | Aligned with InfCodeX behavior |
| Auto repo context | Confidence-gated, 100ms default, top candidate only | Broader cached preturn bundle and routing signals | Primarily tool selection/guidance | InfCodeX remains deeper |
| Parser depth | Python AST; TS/JS/Go tree-sitter active | TypeScript compiler, Lezer/fallback, broader analyzer depth | Broad tree-sitter language/query catalog | Production for declared languages; narrower breadth |
| Runtime | Workspace shared index/cache/watcher, generation invalidation, bounded fallback | Mature worker/cache/wait runtime | Scanner, watcher, state manager, cache | Core lifecycle aligned |
| Semantic retrieval | Optional in-memory prototype, identity-bound results | Structural semantic query, not embedding-first | Incremental embedding pipeline and persistent vector backends | Experimental gap vs OpenCode |
| Tool exposure | Unavailable semantic tool hidden per run | Capability/runtime-aware repo tools | Semantic tool exposed when indexing is configured | Aligned contract |
| Behavioral evaluation | Repeated real-model trajectories and complete failure traces | Same-condition result unavailable | Same-condition result unavailable | Reference behavioral parity unavailable |

## Capability Assessment

### Feature Coverage

Core repository retrieval coverage is now present: identity-based structural
context, deterministic routing, bounded evidence injection, parser-backed
Python/TS/JS/Go, workspace lifecycle, and optional semantic localization.
InfCodeX-style structural lookup is aligned by behavior, not filename.

### Implementation Depth

Structural retrieval and workspace runtime are production depth for the tested
languages. InfCodeX still has deeper analyzer/preturn materialization. OpenCode
still leads semantic implementation depth through incremental chunk state,
embedding caches, provider breadth, and persistent vector storage.

### Behavioral Effectiveness

nzcoder is measured under a real configured coding model. Structural policy
and semantic policy both localize the long vocabulary-mismatch fixtures; the
embedding experiment reduces reads, wrong reads, and turns. Reference
behavioral effectiveness is **insufficient evidence**, not estimated.

## Remaining Core Gaps

Against InfCodeX, the remaining retrieval gaps are richer preturn bundle
materialization, broader analyzer depth, and reference-side behavioral evidence.
The core structural retrieval path is mostly aligned.

Against OpenCode/Kilo, the real gaps are production incremental semantic
indexing, persistent local vector state, wider parser language coverage, and
commercial/external codebase-search integrations where applicable. WarpGrep or
Morph-backed search is an external service advantage, not a mandatory local
runtime primitive.

Web search and persistent PTY remain later gaps and were intentionally not
implemented in this phase.

## Verification

- Full suite: `1843 passed, 7 warnings in 117.02s`.
- Focused retrieval/LSP/runtime resume suite: `82 passed` before the final
  calibration additions; final retrieval-policy suite: `53 passed`.
- Ruff: all changed Python modules, scripts, and focused tests passed.
- `py_compile`: all changed runtime/evaluation scripts passed.
- The seven warnings are Python 3.13 multiprocessing `fork()` deprecations in
  existing fork-based tests; no test failed.
