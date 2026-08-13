# Core Coding Capability Sprint Implementation Plan

> Execute with test-driven development and verify every cluster independently.

## Cluster 1: Repository Intelligence V3

1. Add failing tests for persisted call edges, caller/callee lookup, process context,
   incremental invalidation, and changed-scope propagation.
2. Extend `nz_coder/intelligence/code_index.py` with compatible metadata, call schema,
   AST call extraction/resolution, and bounded context query APIs.
3. Compose symbol/call evidence into repository graph and impact output without
   replacing existing APIs.
4. Run focused index/graph/impact tests and static checks.

## Cluster 2: Tool/Context Scale V2

1. Add failing tests for low-pressure full exposure, pressure-triggered deferral,
   unlock preservation, 20/50/100/200-tool catalogs, aggregate batch limits, named
   head/tail policies, durable recovery, and call/result ordering.
2. Add a pressure snapshot to exposure planning while retaining old call compatibility.
3. Add batch allocation and tool-specific projection to the canonical result projector;
   wire it once in production result consumption.
4. Run focused tool-platform and runtime projection tests.

## Cluster 3: Capability benchmark and trajectory metrics

1. Add failing tests for deterministic A-H manifests and JSONL metric aggregation.
2. Implement `nz_coder/evaluation/core_capability` with fixtures, runner, metrics,
   duplicate/backtracking/verification-loop detectors, and separated score dimensions.
3. Exercise repository intelligence, scale projection, verification recovery, and
   child/conflict accounting through production APIs.
4. Generate a local evidence report and update the development log.

## Final verification

Run focused tests, then the relevant wider suite, Ruff, compile checks, and the local
core-capability benchmark. Record exact commands and outcomes; mark any unmeasured
behavior `unknown`.
