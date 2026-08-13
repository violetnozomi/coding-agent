#!/usr/bin/env python3
"""Measure direct embedding ranking quality on vocabulary-mismatch fixtures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from nz_coder.evaluation.behavioral import _FIXTURES
from nz_coder.intelligence.semantic import sentence_transformer_provider
from nz_coder.intelligence.service import RepoIntelligenceService


def _case(case_id: str, model: str, limit: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"nz-semantic-quality-{case_id}-"))
    service = None
    try:
        task = _FIXTURES[case_id](root)
        service = RepoIntelligenceService(root)
        service.prewarm(max_files=1_000).result(timeout=30)
        service.configure_semantic(sentence_transformer_provider(model))
        result = service.semantic_search(task.prompt, limit=limit, wait_budget_ms=0)
        expected = set(task.expected_files)
        items = [
            {
                "rank": rank,
                "file": str(item.get("file") or ""),
                "score": float(item.get("score") or 0.0),
                "expected": str(item.get("file") or "") in expected,
            }
            for rank, item in enumerate(result.get("items") or (), start=1)
        ]
        correct_scores = [item["score"] for item in items if item["expected"]]
        wrong_scores = [item["score"] for item in items if not item["expected"]]
        return {
            "case_id": case_id,
            "query": task.prompt,
            "expected_files": list(task.expected_files),
            "items": items,
            "recall_at_limit": len({
                item["file"] for item in items if item["expected"]
            }) / max(1, len(expected)),
            "top_is_correct": bool(items and items[0]["expected"]),
            "best_correct_score": max(correct_scores, default=0.0),
            "best_wrong_score": max(wrong_scores, default=0.0),
            "correct_wrong_margin": (
                max(correct_scores, default=0.0) - max(wrong_scores, default=0.0)
            ),
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
    parser.add_argument("--cases", default="I,I2,I3,I4,IS")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    case_ids = [item.strip().upper() for item in args.cases.split(",") if item.strip()]
    payload = {
        "benchmark": "semantic-direct-retrieval-quality-v2",
        "model": args.model,
        "limit": max(1, int(args.limit)),
        "results": [
            _case(case_id, args.model, max(1, int(args.limit)))
            for case_id in case_ids
        ],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
