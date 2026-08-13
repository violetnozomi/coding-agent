# NZ-Coder memory guide

_Inspect, approve, reject, and audit governed durable memory._

---

## 📋 Memory layers

NZ-Coder separates short-lived scratchpad state, durable Session context, and
governed long-term memory. Automatic extraction does not write high-impact facts
directly into long-term memory; it creates a proposal with provenance,
confidence, risk, reason, and fingerprint.

## 🔍 Review workflow

```text
/memory
/memory-review
```

The CLI control plane exposes pending proposals, candidate detail, source
Session/message, confidence, risk, approve, reject, and the append-only review
ledger. The terminal and CLI use `MemoryControlPlane`; they do not edit memory
files directly.

Accepted memories can be curated through their owner:

```text
nz-coder memory edit NAME --description "..." --content "..."
nz-coder memory delete NAME --confirm
/memory edit NAME {"description":"...","content":"..."}
/memory delete NAME confirm
```

These operations call the Session-owned `MemoryManager`; deletion requires an
explicit confirmation token and neither interface writes memory files directly.

Approval is compare-and-apply. If a proposal changes or another client resolves
it after inspection, approval of the stale fingerprint is rejected. Low
confidence or prompt-injection-shaped candidates fail closed.

## ⚠️ Current boundary

Pending list, inspect, approve, reject, stale-version protection, ledger, and
accepted-memory edit/delete are covered by executable contracts. Curation is a
deliberately explicit CLI/command workflow rather than an unrestricted file
editor.
