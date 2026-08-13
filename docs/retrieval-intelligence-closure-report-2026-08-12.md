# Retrieval Intelligence Closure

Date: 2026-08-12

This phase closes the retrieval-policy and parser-production gaps without
redesigning AgentRunner, Session, SubAgent, Memory, Skills, MCP, planning, or
verification architecture. The implementation is measured separately as
feature coverage, implementation depth, and behavioral effectiveness.

## What Changed

- Added deterministic `RepoRetrievalPolicy` with task routing for known files,
  known symbols, structural questions, changed-code impact, and business-language
  vocabulary mismatch. It exposes a bounded `RepoRoutingSignal`, explicit tool
  guidance, and optional first-turn context. Low-confidence candidates are not
  injected. See [retrieval_policy.py](../nz_coder/intelligence/retrieval_policy.py:1)
  and the prompt integration in [loop.py](../nz_coder/runtime/loop.py:1153).
- Added a single structural/semantic retrieval boundary. The optional semantic
  index reuses indexed symbol spans and binds every result to `symbol_id` and
  `module_id`; it has generation-aware cache invalidation and a dependency-free
  in-memory vector store. See [semantic.py](../nz_coder/intelligence/semantic.py:1)
  and [semantic_search.py](../nz_coder/tools/semantic_search.py:1).
- Added bounded semantic-provider loading. Model download/load failures are
  recorded and future requests immediately fall back to structural lookup,
  grep, or LSP. A slow provider cannot block the workspace worker or process
  shutdown.
- Added an independent `nz-coder doctor --repo-intelligence-only` probe. The
  current installation reports Python AST, TS/JS/Go tree-sitter, watchfiles,
  and LSP augmentation as available. See [doctor.py](../nz_coder/doctor.py:57).
- Declared TS/JS/Go parser wheels and watchfiles in both `pyproject.toml` and
  `requirements.txt`; `sentence-transformers` is isolated in the optional
  `semantic-experiment` extra. CI now verifies both parser and honest lexical
  fallback tiers in [.github/workflows/repo-intelligence.yml](../.github/workflows/repo-intelligence.yml:1).
- Added behavior manifest Case I, retrieval strategy metrics, localization
  timing/precision, semantic calls, fallback counts, and production retrieval
  matrices. Existing A-H contract coverage remains intact.

## Verification

`python -m pytest -q` completed with **1794 passed, 7 warnings**. The focused
retrieval/parser/runtime set completed with 51 passed. Controlled A-I behavior
coverage is 9/9; controlled results are mechanism/regression evidence, not
real-model intelligence.

Runtime/parser contracts cover:

- Main plus two child agents sharing one workspace service, generation, cache,
  and watcher; a separate worktree receives a separate service/index.
- Bounded query timeout, low-confidence no-injection, semantic cache invalidation
  after incremental generation changes, and explicit unavailable-provider fallback.
- Python/TS/JS/Go parser selection and alias/qualified-call fixtures, plus a
  fallback-tier test with tree-sitter unavailable.

## Production Behavioral Evidence

All production runs used the same configured model (`openai-compatible/deepseek-v4-flash`),
provider-default reasoning, temperature 0, and three repetitions per group.
InfCodeX and OpenCode were not run under the same model/budget; reference
behavioral parity is therefore unavailable rather than estimated.

### Structural retrieval strategy matrix

Evidence: [production-retrieval-matrix.json](evidence/retrieval-intelligence-production-structural-2026-08-12/production-retrieval-matrix.json)
(`60` runs: A/B/C/D/I x four strategies x three repetitions).

| Case | Strategy | Success | Mean localization turn | Mean reads | Retrieval precision | Mean turns |
|---|---|---:|---:|---:|---:|---:|
| A | tool-only | 3/3 | 1.00 | 5.00 | 0.600 | 7.67 |
| A | guidance | 3/3 | 1.00 | 5.00 | 0.600 | 6.33 |
| A | auto-context | 3/3 | 2.33 | 5.00 | 0.600 | 7.67 |
| A | policy | 3/3 | 3.00 | 5.00 | 0.600 | 9.33 |
| D | tool-only | 3/3 | 2.00 | 3.00 | 0.800 | 5.67 |
| D | guidance | 3/3 | 2.33 | 2.00 | 1.000 | 6.33 |
| D | auto-context | 3/3 | 2.00 | 2.00 | 1.000 | 7.33 |
| D | policy | 3/3 | 3.00 | 2.00 | 1.000 | 5.67 |
| I | tool-only | 3/3 | 1.00 | 5.33 | 0.790 | 7.33 |
| I | guidance | 3/3 | 1.00 | 6.67 | 0.624 | 10.00 |
| I | auto-context | 3/3 | 1.00 | 5.67 | 0.711 | 6.33 |
| I | policy | 3/3 | 1.00 | 5.00 | **0.857** | 6.33 |

Case B was 0/3 for every strategy because all runs hit the same existing
verification-policy failure after correct impact reads; it is not a retrieval
strategy difference. Case I, the vocabulary-mismatch gate, succeeds 12/12
across structural strategies. The current evidence favors bounded policy or
guidance over unconditional auto-context: auto-context adds prompt work but
does not improve Case I success or localization.

### Historical RI exposure A/B

Evidence: [production-agent-behavior-matrix.json](evidence/retrieval-intelligence-production-modes-2026-08-12/production-agent-behavior-matrix.json)
(`60` runs: OFF/current/V3/lookup x A/B/C/D/I x three repetitions).

Across all cases, success was 100% except Case B's shared verification failure.
For the non-B groups, current/lookup reduced mean reads and turns versus OFF;
the V3 and lookup tiers remain behaviorally useful. The matrix is an A/B
efficiency result, not a claim that Repo Intelligence alone fixes verification.

### Semantic experiment

The provider-neutral prototype is structurally correct: deterministic fixture
tests find a business-intent query, return the correct file span, and preserve
symbol/module identity; cache invalidation follows index generation. The
production provider extra installed successfully, but the first
`all-MiniLM-L6-v2` load could not complete a bounded HuggingFace probe and had
no offline local cache. Probe evidence is recorded in
[semantic-provider-probe.json](evidence/retrieval-intelligence-semantic-provider-probe-2026-08-12.json).

The offline semantic matrix contains 54 production runs (structural controls
plus semantic tool exposure on A/D/I, three repetitions). Semantic calls were
observable and unavailable-provider responses returned explicit fallback; Case
I policy precision was 0.867 in this run, but strategy/control differences were
not stable enough to attribute that result to embeddings, and there was no
demonstrated success/turn advantage. This is graceful-degradation evidence, not
embedding-quality evidence.

**Decision: retain semantic retrieval as an optional experimental capability,
not a core dependency.** A real embedding model must be preloaded or supplied
by a configured provider before a future domain-diverse benchmark can measure
its value. Do not add a vector database or make semantic retrieval mandatory.

## Three-Dimension Parity

| Dimension | nzcoder | InfCodeX | OpenCode/Kilo | Verdict |
|---|---|---|---|---|
| Feature Coverage | Structural lookup, deterministic routing, bounded auto-context, optional semantic protocol, Python/TS/JS/Go parser tiers, workspace RI runtime | Structural semantic index, auto preturn context, TypeScript/Lezer/fallback analyzers | Structural indexing plus semantic-search tool and embedding/indexing pipeline | Core structural retrieval is covered; embedding remains optional by evidence |
| Implementation Depth | Identity-bound chunks, generation cache, wait budget, fallback, parser doctor/CI, shared workspace service | Mature persisted semantic index/runtime and richer TypeScript analysis | Chunk/index/search abstractions and embedding-backed retrieval | Aligned in contracts/runtime safety; parser/type-depth and persisted semantic catalog remain shallower |
| Behavioral Effectiveness | Measured nzcoder A/B above; controlled A-I 9/9; reference same-model result unavailable | Unavailable under the same model/task/budget | Unavailable under the same model/task/budget | nzcoder measured; reference behavioral parity: insufficient evidence |

## Remaining Real Gaps

1. A production embedding model/cache is not available in this environment, so
   semantic quality is unmeasured. Keep it experimental until a preloaded model
   produces repeated gains on vocabulary-diverse fixtures.
2. TypeScript/JavaScript/Go are parser-based, but do not claim compiler/type
   checker precision for every dynamic/member or cross-package case; unresolved
   targets remain explicit.
3. The shared verification-policy failure exposed by Case B is independent of
   retrieval and should be handled in a separate verification phase.
4. Web search and persistent PTY remain OpenCode-specific later gaps and were
   intentionally outside this retrieval closure.

The phase is complete at the retrieval/parser/runtime boundary. The next work
should be driven by the verification failure or by a repeatable, preloaded
semantic benchmark, not by adding another parallel retrieval architecture.
