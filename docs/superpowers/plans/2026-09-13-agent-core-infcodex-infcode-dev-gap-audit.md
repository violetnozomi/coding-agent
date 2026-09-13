# Agent Core / InfCodeX / infcode-dev Gap Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an evidence-backed three-way capability audit and close the first actionable gaps in capability reporting and Skills governance diagnostics.

**Architecture:** Keep NZ-Coder's existing runtime owners as the source of truth. Add read-only, deterministic projections for product capability status and skill discovery diagnostics, then document deferred product-host gaps separately from core-runtime parity.

**Tech Stack:** Python 3.9+, dataclasses/enums, JSON-compatible dictionaries, pytest, standard library only.

**Spec:** `docs/superpowers/specs/2026-09-13-agent-core-infcodex-infcode-dev-gap-audit-design.md`

## Global Constraints

- Do not introduce an Agent framework, new runtime owner, database, or external dependency.
- Preserve existing public names and return shapes; new APIs are additive.
- Python 3.9+ compatibility and `from __future__ import annotations` remain required.
- Diagnostics and snapshots must be deterministic, bounded, JSON-compatible, and secret-free.
- Every behavior change needs focused pytest coverage and fresh verification evidence.
- Keep core-runtime parity separate from infcode-dev host-product parity.

---

### Task 1: Publish the canonical three-way audit and manifest

**Files:**
- Create: `docs/agent-core-infcodex-infcode-dev-gap-audit.md`
- Create: `docs/evidence/agent-core-parity-2026-09-13.json`

**Interfaces:**
- Produces a human-readable matrix and a machine-readable list of gap records.

**Steps:**

- [ ] Record the exact evidence roots and benchmark snapshot distinction.
- [ ] Add rows for core runtime, provider metadata, tool exposure, skills/MCP, memory, workflow, SDK/events, product hosts, indexing, and UI transport.
- [ ] Give every partial/planned row an owner file set, acceptance test, and reason it is not closed now.
- [ ] Validate the JSON with the standard library and check for absolute workspace paths or secrets.
- [ ] Commit the audit artifacts.

**Verification:**

```bash
python -m json.tool docs/evidence/agent-core-parity-2026-09-13.json >/dev/null
python - <<'PY'
from pathlib import Path
text = Path('docs/agent-core-infcodex-infcode-dev-gap-audit.md').read_text()
assert 'InfCodeX' in text and 'infcode-dev' in text
assert '0.8667' in text
PY
```

### Task 2: Add a host-neutral capability snapshot

**Files:**
- Modify: `nz_coder/runtime/execution/product_surfaces.py`
- Create: `tests/runtime/test_product_capability_snapshot.py`

**Interfaces:**
- Add `capability_snapshot(surface)` returning sorted JSON-compatible rows with capability name, status, evidence owner, and parity note.
- Keep `capability_fingerprint(surface)` unchanged.

**Steps:**

- [ ] Define the immutable capability metadata table in one place.
- [ ] Ensure every `ProductSurface` receives the same capability names while status/provenance remain explicit.
- [ ] Return defensive copies so callers cannot mutate module state.
- [ ] Test all surfaces, deterministic ordering, unknown surface rejection, and mutation isolation.
- [ ] Run the focused tests and commit.

**Verification:**

```bash
pytest -q tests/runtime/test_product_capability_snapshot.py
python -m compileall -q nz_coder/runtime/execution/product_surfaces.py
```

### Task 3: Add Skills governance diagnostics

**Files:**
- Modify: `nz_coder/state/skills.py`
- Modify: `tests/test_skill_governance.py`

**Interfaces:**
- Add `SkillLoader.diagnostics()` returning bounded rows for source roots, loaded/conditional/disabled skills, shadowed duplicates, and parse errors.

**Steps:**

- [ ] Track invalid header reads without loading skill bodies.
- [ ] Track duplicate names shadowed by project > user > bundled precedence.
- [ ] Keep diagnostics stable across repeated calls and reloads.
- [ ] Do not expose skill body content, environment values, or unbounded paths.
- [ ] Add tests for valid, duplicate, conditional, disabled, malformed, and empty directories.
- [ ] Run the focused skills/extension tests and commit.

**Verification:**

```bash
pytest -q tests/test_skill_governance.py tests/test_extensions.py
python -m compileall -q nz_coder/state/skills.py
```

### Task 4: Reconcile documentation with implementation evidence

**Files:**
- Modify: `docs/agent-core-infcodex-infcode-dev-gap-audit.md`
- Modify: `docs/evidence/agent-core-parity-2026-09-13.json`
- Create: `.superpowers/sdd/2026-09-13-agent-core-infcodex-infcode-dev-gap-audit/progress.md`

**Interfaces:**
- The audit status must match source symbols and passing focused tests.

**Steps:**

- [ ] Mark the capability snapshot and Skills diagnostics rows implemented only after their tests pass.
- [ ] Preserve deferred product-host and indexing rows with their evidence boundaries.
- [ ] Run the changed-module test set, compileall, Ruff if available, and a final diff/status check.
- [ ] Commit the reconciled audit.

**Verification:**

```bash
pytest -q tests/runtime/test_product_capability_snapshot.py tests/test_skill_governance.py tests/test_extensions.py
python -m compileall -q nz_coder/runtime/execution/product_surfaces.py nz_coder/state/skills.py
git diff --check
```
