# Native Memory and Verifier Contexts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace host-shaped Memory and Verifier service inputs with focused execution contexts.

**Architecture:** Add core contracts, legacy adapters, and update only the two service call chains. Native composition will consume the same contracts later.

**Tech Stack:** Python 3.9+, asyncio, pytest, standard library.

## Global Constraints

- Preserve MemoryManager and memory-control behavior.
- Preserve reflection/stop-hook behavior.
- Do not add methods or state to AgentLoop.
- Production services must not retain or inspect a broad host.

### Task 1: Memory context

- [x] Write failing focused service tests.
- [x] Implement `MemoryExecutionContext` and `MemoryRecallState`.
- [x] Add legacy adapter and migrate prompt/finalize calls.
- [x] Run memory and architecture regressions.

### Task 2: Verifier context

- [x] Write failing override/default verifier tests.
- [x] Implement `VerificationExecutionContext`.
- [x] Add legacy adapter and migrate Runner policy call.
- [x] Run verifier, Runner and architecture regressions.

### Task 3: Closure

- [x] Update capability documentation and learning log.
- [x] Run full pytest, Ruff, compileall and diff checks.
- [x] Mark executable checkboxes complete from fresh evidence.
