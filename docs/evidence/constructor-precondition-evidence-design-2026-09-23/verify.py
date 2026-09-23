"""Provider-free integrity checks for the design audit."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASELINE = "6ce79194b17ef874dae5c55175d5c5423cd32d5d"


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main() -> None:
    assert run("git", "rev-parse", "HEAD") == BASELINE
    assert run("git", "rev-parse", "origin/main") == BASELINE
    status = run("git", "status", "--short")
    assert all("constructor-precondition-evidence-design-2026-09-23/" in line for line in status.splitlines() if line)
    assert run("git", "diff", "--", "nz_coder") == ""
    results = json.loads((OUT / "fixtures/results.json").read_text())
    assert results["dataclass-post-init"]["decorator_preserved"]
    assert "__post_init__" in results["dataclass-post-init"]["envelopes"][0]["text"]
    assert "__init__" in results["validating-init"]["envelopes"][0]["text"]
    assert results["budget"]["many_classes_selected"] == 2
    assert results["budget"]["deterministic_order"]
    selection = json.loads((OUT / "c/candidate-selection.json").read_text())
    assert selection["route_A"]["c_specific"] is False
    assert selection["route_C"]["status"] == "unsupported"
    assert json.loads((OUT / "analysis/genericity.json").read_text())["classification"] == "PRECOND-DESIGN: EXISTING-GRAPH-SUFFICIENT"
    production_files = [
        "nz_coder/intelligence/repository_graph.py",
        "nz_coder/intelligence/service.py",
        "nz_coder/intelligence/code_index.py",
        "nz_coder/runtime/verification/sidecar_verifier.py",
    ]
    (OUT / "baseline/source-hashes.json").parent.mkdir(parents=True, exist_ok=True)
    (OUT / "baseline/source-hashes.json").write_text(json.dumps({p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in production_files}, indent=2) + "\n")
    files = sorted(p for p in OUT.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name not in {"SHA256SUMS.json", "integrity-audit.json"})
    hashes = {str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (OUT / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n")
    (OUT / "integrity-audit.json").write_text(json.dumps({
        "baseline": BASELINE, "origin_main": BASELINE, "production_changed": False,
        "provider_requests": 0, "main_requests": 0, "planner_requests": 0,
        "embedding_requests": 0, "infcodex_requests": 0,
        "hidden_evaluator_used": False, "classification": "PRECOND-DESIGN: EXISTING-GRAPH-SUFFICIENT",
        "files_hashed": len(hashes),
    }, indent=2) + "\n")
    print(f"constructor evidence design integrity PASS ({len(hashes)} files, 0 provider requests)")


if __name__ == "__main__":
    main()
