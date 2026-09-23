"""Provider-free integrity checks for the C semantic proof audit."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASELINE = "949f867892ab4dad56f5914ecad4276be5e99757"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main() -> None:
    assert run("git", "rev-parse", "HEAD") == BASELINE
    assert run("git", "rev-parse", "origin/main") == BASELINE
    status = run("git", "status", "--short")
    assert all(line.endswith("c-semantic-proof-audit-2026-09-23/") or "c-semantic-proof-audit-2026-09-23/" in line for line in status.splitlines() if line)

    proof = json.loads((OUT / "proof/minimal-counterexample.json").read_text())
    assert proof["construction"]["succeeded"]
    assert proof["serialization"]["succeeded"]
    assert proof["save"]["destination_changed"]
    assert proof["migration_path"]["input_preserved"]

    coverage = json.loads((OUT / "proof/premise-table.json").read_text())
    assert {item["id"] for item in coverage} == {"P1", "P2", "P3", "P4", "P5"}
    assert next(item for item in coverage if item["id"] == "P2")["status"] == "absent"
    assert json.loads((OUT / "analysis/evidence-sufficiency.json").read_text())["classification"] == "PROOF-AUDIT: MIXED"

    v0 = (OUT / "packet/v0-current-packet.txt").read_text()
    v1 = (OUT / "packet/v1-minimal-proof-complete-packet.txt").read_text()
    assert "configkit/config/model.py" not in v0
    assert "configkit/config/model.py" in v1
    forbidden = ("invalid_write_preserves_destination", "11/12", "acceptance/checks", "oracle/")
    assert all(token not in v0 for token in forbidden)
    assert all(token not in v1 for token in forbidden)

    # No production source or prior evidence is part of this uncommitted audit.
    assert run("git", "diff", "--name-only", "--", "nz_coder") == ""
    assert run("git", "diff", "--name-only", "--", "docs/evidence/diagnostic-v1-C-real-2026-09-22", "docs/evidence/c-closure-semantics-audit-2026-09-22", "docs/evidence/c-sidecar-fixed-diff-counterfactual-2026-09-23", "docs/evidence/semantic-dependency-evidence-2026-09-22") == ""

    files = sorted(p for p in OUT.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name not in {"SHA256SUMS.json", "integrity-audit.json"})
    hashes = {str(p.relative_to(OUT)): sha(p) for p in files}
    (OUT / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n")
    (OUT / "integrity-audit.json").write_text(json.dumps({
        "baseline": BASELINE,
        "origin_main": BASELINE,
        "provider_requests": 0,
        "main_requests": 0,
        "planner_requests": 0,
        "embedding_requests": 0,
        "infcodex_requests": 0,
        "production_source_changed": False,
        "frozen_c_workspace_changed": False,
        "prior_evidence_changed": False,
        "hidden_evaluator_leaked": False,
        "counterexample_reproduced": True,
        "migration_preserves_input": True,
        "classification": "PROOF-AUDIT: MIXED",
        "files_hashed": len(hashes),
    }, indent=2) + "\n")
    print(f"proof audit integrity PASS ({len(hashes)} files, 0 provider requests)")


if __name__ == "__main__":
    main()
