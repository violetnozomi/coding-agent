# Contract-Led Long-Task Convergence Design

## Goal

Reduce multi-file coding tasks from repeatedly exhausting the 20-call hard cap by making completion, first-turn repository evidence, verification scheduling, closure budget, context freshness, and provider tool schemas deterministic runtime concerns.

## Scope

This phase implements the six changes approved in the 2026-08-14 cross-project audit:

1. TaskContract, RequirementLedger, and CompletionGate.
2. ImplementationBundle and ProjectExecutionFacts.
3. Layered budget-zone verification instead of repeated full acceptance.
4. A 13-normal-call plus 2-closure-call nominal budget with a 20-call emergency cap.
5. Mutation-aware semantic supersession in provider message projection.
6. ProviderSchemaAdapter and recursive tool-schema linting.

It does not add another planner call, a fine-grained linear workflow state machine, a new compaction system, default explore subagents, or additional patch tools.

## Architecture

The existing planning call returns one JSON object containing both a short plan and a TaskContract. Runtime validation normalizes unique requirement IDs and workspace-relative artifact paths. RuntimeState remains the owner of objective execution facts; a separate RequirementLedger owns requirement progress and evidence. Deterministic file/test/verification evidence can promote requirements to candidate or satisfied, while semantic behavior requirements require verification evidence before satisfaction.

An ImplementationBundle is generated only for moderate/complex coding tasks with multiple artifacts. It combines the contract, ProjectExecutionFacts, high-confidence repository candidates, and bounded snippets. It is injected on the first model turn and remains bounded to 1,500–3,000 estimated tokens.

Budget zones become scheduling signals. Yellow requests cheap/static checks, orange requests targeted affected checks, red requests convergence and may run exact acceptance only when no hard requirement remains pending, and completion always runs the exact user contract once per mutation generation.

The nominal call budget is 15: 13 normal calls and 2 closure calls. The hard cap remains the configured maximum, currently 20. Closure calls restrict model-facing tool schemas to narrow reads, edits, diff, and targeted verification; they prohibit broad repository exploration.

Durable Session messages retain full tool evidence. Provider projection replaces stale reads and superseded verification outputs with short generation markers after later mutations or passing verification evidence make them obsolete.

Tool definitions remain canonical. A provider presentation adapter deep-copies and simplifies schemas for OpenAI-compatible/DeepSeek, Anthropic, and Gemini without changing handlers. A recursive linter detects object properties that are required by handler contracts but absent from nested JSON Schema `required` lists.

## Completion Semantics

CompletionGate allows normal completion when there is no contract or all hard requirements are satisfied for the current mutation generation. If deterministic evidence is incomplete, it returns a bounded list of unresolved requirements and requests a narrow repair turn. A user-declared exact acceptance command remains mandatory at natural completion.

Requirement states are `pending`, `in_progress`, `candidate`, `satisfied`, and `blocked`. File changes establish artifact evidence and normally promote requirements to candidate. Passing targeted or acceptance verification can satisfy test, verification, compatibility, and mixed behavior requirements whose expected artifacts have evidence. Documentation requirements may be satisfied deterministically by a changed expected documentation artifact.

## Error Handling and Compatibility

Malformed planner JSON falls back to the existing Markdown plan behavior and leaves the TaskContract empty, so planning failures never block the Agent loop. Restored RuntimeState accepts missing new fields. All new paths are normalized relative to the active workspace and paths that escape it are rejected. Existing tool names, handler signatures, and SWE-bench strict behavior remain compatible.

## Verification

Each component receives focused unit coverage through a red-green cycle. Runner tests verify zone scheduling, closure restrictions, and completion gating. Projection tests verify that durable messages remain intact while provider messages omit stale evidence. Provider tests verify schema adaptation and lint findings. The final gate runs focused tests, the full pytest suite, Ruff on changed Python files, `git diff --check`, and a local fake-provider end-to-end run. Expensive live-model A/B runs remain a separate follow-up because they consume external API budget.
