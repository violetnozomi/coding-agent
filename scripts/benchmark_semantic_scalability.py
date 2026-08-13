#!/usr/bin/env python3
"""Measure the optional semantic prototype's rebuild cost without model I/O."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource
import shutil
import tempfile
import time

from nz_coder.intelligence.service import RepoIntelligenceService


class CountingEmbeddingProvider:
    identity = "benchmark/content-hash-v1"

    def __init__(self) -> None:
        self.embedded_texts = 0

    def embed(self, texts):
        self.embedded_texts += len(texts)
        result = []
        for text in texts:
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            result.append([byte / 255.0 for byte in digest[:16]])
        return result


def _write_fixture(root: Path, files: int) -> None:
    for index in range(files):
        target = root / f"packages/pkg_{index // 100:03d}/module_{index:05d}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"def operation_{index}(value):\n    return value + {index}\n",
            encoding="utf-8",
        )


def _measure(files: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"nz-semantic-{files}-"))
    try:
        _write_fixture(root, files)
        service = RepoIntelligenceService(root)
        service.prewarm(max_files=files + 10).result(timeout=120)
        provider = CountingEmbeddingProvider()
        service.configure_semantic(provider)

        started = time.perf_counter()
        initial = service.semantic_search("find an operation", wait_budget_ms=0)
        initial_ms = (time.perf_counter() - started) * 1000
        initial_metrics = dict(service.metrics()["semantic_index"])

        changed = root / "packages/pkg_000/module_00000.py"
        changed.write_text(
            "def operation_0(value):\n    return value + 1000\n",
            encoding="utf-8",
        )
        service._apply_incremental(("packages/pkg_000/module_00000.py",), files + 10)
        started = time.perf_counter()
        updated = service.semantic_search("find an operation", wait_budget_ms=0)
        update_ms = (time.perf_counter() - started) * 1000
        updated_metrics = dict(service.metrics()["semantic_index"])
        rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        service.close()
        return {
            "files": files,
            "initial_query_ms": round(initial_ms, 3),
            "initial_chunks_embedded": initial_metrics["last_embedded_chunks"],
            "one_file_update_query_ms": round(update_ms, 3),
            "one_file_update_chunks_embedded": updated_metrics["last_embedded_chunks"],
            "total_chunks_embedded": updated_metrics["total_embedded_chunks"],
            "max_rss_kib": int(rss_kib),
            "initial_embedding": bool(initial.get("embedding")),
            "updated_embedding": bool(updated.get("embedding")),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _chunk_coverage_probe() -> dict:
    root = Path(tempfile.mkdtemp(prefix="nz-semantic-coverage-"))
    try:
        target = root / "policy.py"
        target.write_text(
            'PAYMENT_RETENTION_POLICY = "durable"\n\n'
            "def save(record):\n    return record\n",
            encoding="utf-8",
        )
        service = RepoIntelligenceService(root)
        service.prewarm(max_files=10).result(timeout=30)
        provider = CountingEmbeddingProvider()
        service.configure_semantic(provider)
        service.semantic_search("retention policy", wait_budget_ms=0)
        chunks = service._semantic_index._chunks(service.index.snapshot().files)
        rendered = "\n".join(chunk.code_chunk for chunk in chunks)
        service.close()
        return {
            "module_constant_covered": "PAYMENT_RETENTION_POLICY" in rendered,
            "function_covered": "def save" in rendered,
            "chunk_count": len(chunks),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sizes", default="100,500,2000")
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    payload = {
        "benchmark": "semantic-prototype-scalability",
        "provider": "deterministic-mechanics-only",
        "results": [_measure(size) for size in sizes],
        "chunk_coverage": _chunk_coverage_probe(),
        "conclusion_scope": (
            "Measures rebuild mechanics and resource cost only; it is not evidence "
            "of embedding retrieval quality."
        ),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
