# Agent Core Diagnostic Suite v1

These are three orthogonal diagnostic tasks, not a benchmark or a success-rate
estimate. This phase builds and tests instruments only: **0 paid model requests**.
No NativeSDKRunner, AgentRunner, Provider, verifier or InfCodeX is launched here.

## A: Unknown-location cross-module correlation propagation

Hypothesis: repository discovery can connect a public behavior to its internal
event model, processing service, two sink adapters and CLI without file hints.
Nine behavior checks cover public and indirect calls, JSON/text output, omitted
and empty IDs, CLI formats, compatibility and README examples.
Healthy behavior finds the relevant chain, propagates values, checks compatibility,
adds tests/docs and reaches verified semantic closure. No particular tool order
is prescribed. Classifications: repo-location, caller-propagation, verification,
documentation obligation or terminal failure. This is not Money formatting.

## B: Deterministic failure-driven retry scheduling

Hypothesis: an Agent can use a real boundary-test failure to make a targeted repair
and reverify the changed generation. Single-worker retries go to the queue tail;
worker-scripted enqueue and failure sequences make fairness observable without
threads, race-dependent timing or sleeps. This is not Q's concurrent pool/drain/
cancellation problem. Nine checks cover retries, exact error identity, attempt
counts, strict retryable flag, legacy behavior, FIFO/dynamic work and docs.
Healthy behavior reads actual failure evidence, diagnoses the failed condition,
edits the relevant code, reruns tests and reports the result. A first attempt may
already be correct; neither an initial failure nor a review-tool call is forced.
Classifications: lost failure evidence, blind retry, wrong repair, repeated no
progress, stale verification generation. Those labels need trajectory evidence.

## C: Multi-obligation v1/v2 configuration migration

Hypothesis: original authority and multiple obligations survive a naturally longer
read/edit/verify process. Eighteen purposeful Python modules separate parsing,
validation, serialization, storage, migration, consumers, reporting and commands.
CONFIG_SPEC.md defines both structures, exact error shape and dry-run semantics.
Twelve checks cover v1/v2 reads, v2-only writes, idempotent migration, byte-preserving
preview, validation, CLI, consumers, no-clobber behavior and docs. Oracle leaves
CONFIG_SPEC.md byte-identical. RuntimeState offline capture is tested separately.
Healthy behavior reconciles read/write compatibility, updates CLI/docs/tests,
reverifies current code and closes obligations against the original specification.
Classifications: authority/context loss, compaction issues, omitted obligations,
semantic conflict or false completion. Compaction is possible, never forced.
This has no CAS/revision/concurrency store requirement from historical Storage S.

## Layout and commands

Each task contains task.md (the complete future user request), workspace/ (the
only Agent materialization), acceptance/checks.json, oracle/ (evaluator-only
changed-file overlay), manifest.json and requirement-map.json.
Manifests are evaluator metadata and are never appended to user instructions.
The named checks each run in their own disposable copy with structured
name/pass/exit/stdout/stderr/timeout output. No checks depend on a previous check.

From repository root:

```sh
python tests/evaluation/fixtures/agent_core_diagnostic_v1/validate.py validate all /tmp/diagnostic-validation
python tests/evaluation/fixtures/agent_core_diagnostic_v1/validate.py prepare A_unknown_location /tmp/agent-A
python tests/evaluation/fixtures/agent_core_diagnostic_v1/validate.py acceptance A_unknown_location /tmp/agent-A
```

The acceptance command exits 0 only when every named check passes. Validation
expects baseline tests to pass and initial independent acceptance to fail; it then
applies the oracle and repeats from another clean initial copy. No dependency
installation occurs. Python, pytest, Node >=18 and Linux libseccomp are preflight
requirements. Missing offline isolation must fail, not fall back to online mode.

The validator reuses tests/evaluation/fixtures/offline_exec.py and existing
reference_adapter workspace hashes. This small fixture wrapper is not a parallel
Agent execution framework. Provider capture/version projection remains the prior
Q harness's responsibility in a separately authorized future experiment.

## Isolation and bounds

Every project/check subprocess inherits kernel denial of IPv4/IPv6 sockets and
has a fresh HOME/TMPDIR, fixed hash seed and allowlisted environment without
credentials, proxies or Provider Unix transport. Checks have 8-second deadlines,
project tests 30 seconds; overdue process groups are killed, followed by a bounded
5-second reap. B async checks also have a 2-second completion guard and Node tests
have 1-second deadlines. Timing is a hang guard, never a correctness assertion.
Snapshots reject symlinks and compare before/copy/after hashes. Acceptance runs on
disposable copies, not the live Agent workspace; evaluate only settled versions.
This is network isolation, **not filesystem hermeticity**. Future Agent tools must
be scoped to the materialized workspace; do not run the Agent in this repository.

## Future capture and permission policy (not executed)

See protocol.json and causal-table.schema.json. The schema retains physical
main/auxiliary IDs separately; tools/permission/dispatch/command failures,
generation, requirement/reference/recovery/review/terminal facts and raw Provider
usage. causal-table.json is empty. first_divergence.json is unobserved, not a pass.
No synthetic model rows, guessed verdicts, token values or acceptance timelines.

Reuse the M/Q NativeSDKRunner capture approach and built-in permission guards.
Allow normal workspace file tools, explicit project tests and documented local CLI
commands. No arbitrary shell composition, network, dependency installation, secrets,
or evaluator-only paths. The precise argv policy is frozen in protocol.json;
implementation/wiring of the future paid launcher remains a separate preflight.
Never turn independent acceptance output into an unsolicited model hint.

## Future stop policy

Only after new explicit paid authorization: exactly one real NZ sample each,
sequential A -> freeze/analyze -> B -> freeze/analyze -> C. No resampling, no
InfCodeX, no manual review invocation. Freeze at the first deterministic Core
divergence; stop later tasks if they have no additional causal value. Distinguish
fixture, permission, Provider, coding and Runtime failures. Move any proven Core
break to a separate offline RED phase; do not patch during a real run.

Analyze the first divergence of each individual trajectory. Even three completed
tasks do not justify a success rate, model ranking or general efficiency claim.

## Limits of this instrument

Task/check quotes are mapped explicitly; no known hidden requirement was added.
Finite examples do not prove all possible inputs. Documentation checks are narrow
API/example-token smoke checks, not a semantic prose judge. Meaningfulness of new
Agent-authored tests and final reports still requires semantic review. Oracle
proves solvability, not difficulty for a particular model. No real trajectory,
first failure, compaction, recovery or terminal outcome has been measured here.
