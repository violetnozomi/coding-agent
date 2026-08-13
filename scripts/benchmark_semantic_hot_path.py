#!/usr/bin/env python3
"""Measure cold-workspace semantic evidence under bounded preturn budgets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import statistics
import tempfile

from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy
from nz_coder.intelligence.semantic import sentence_transformer_provider
from nz_coder.intelligence.service import RepoIntelligenceService


QUERY = "fix duplicate invoice retries"


def _fixture(root: Path) -> None:
    for index in range(80):
        target = root / f"domains/slice_{index:03d}/unit.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"def unit_{index}(x): return x\n", encoding="utf-8")
    (root / "ledger").mkdir()
    (root / "ledger/intake.py").write_text(
        "from ledger.reservation import reserve_once\n"
        "def accept_document(document): return reserve_once(document)\n",
        encoding="utf-8",
    )
    (root / "ledger/reservation.py").write_text(
        "def reserve_once(document): "
        "return {'key': document['id'], 'attempts': 1}\n",
        encoding="utf-8",
    )


def _run_once(model: str, budget_ms: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix="nz-semantic-hot-path-"))
    service = None
    try:
        _fixture(root)
        service = RepoIntelligenceService(root)
        service.prewarm(max_files=500)
        service.configure_semantic(sentence_transformer_provider(model))
        initial_status = service.state.status
        decision = RepoRetrievalPolicy(hot_path_ms=budget_ms).decide(
            QUERY, service=service, strategy="policy", semantic_available=True,
        )
        direct = service.semantic_search(QUERY, limit=3, wait_budget_ms=2_000)
        direct_items = list(direct.get("items") or ())
        return {
            "budget_ms": budget_ms,
            "initial_status": initial_status,
            "final_status": service.state.status,
            "elapsed_ms": decision.elapsed_ms,
            "candidate_count": decision.signal.candidate_count,
            "candidate_files": list(decision.signal.candidate_files),
            "evidence_confidence": decision.signal.evidence_confidence,
            "routing_confidence": decision.signal.routing_confidence,
            "fallback_state": decision.signal.fallback_state,
            "auto_context": bool(decision.auto_context),
            "direct_top_score": (
                float(direct_items[0].get("score") or 0.0) if direct_items else 0.0
            ),
            "direct_top_files": [
                str(item.get("file") or "") for item in direct_items
            ],
        }
    finally:
        if service is not None:
            service.close()
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model", default="sentence-transformers/all-MiniLM-L6-v2",
    )
    parser.add_argument("--budgets", default="50,100,250,500")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    budgets = [int(value) for value in args.budgets.split(",") if value.strip()]
    repetitions = max(1, int(args.repetitions))
    runs = [
        _run_once(args.model, budget)
        for budget in budgets
        for _repetition in range(repetitions)
    ]
    aggregates = []
    for budget in budgets:
        selected = [run for run in runs if run["budget_ms"] == budget]
        aggregates.append({
            "budget_ms": budget,
            "runs": len(selected),
            "candidate_rate": sum(run["candidate_count"] > 0 for run in selected) / len(selected),
            "auto_context_rate": sum(run["auto_context"] for run in selected) / len(selected),
            "median_elapsed_ms": round(statistics.median(
                run["elapsed_ms"] for run in selected
            ), 3),
            "median_evidence_confidence": round(statistics.median(
                run["evidence_confidence"] for run in selected
            ), 3),
            "median_direct_top_score": round(statistics.median(
                run["direct_top_score"] for run in selected
            ), 3),
        })
    payload = {
        "benchmark": "semantic-cold-hot-path-budget",
        "model": args.model,
        "query": QUERY,
        "repetitions": repetitions,
        "runs": runs,
        "aggregates": aggregates,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
