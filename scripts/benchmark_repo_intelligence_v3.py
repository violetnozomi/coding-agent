#!/usr/bin/env python3
"""Measure identity-index cold/warm/update/query behavior at repository scale."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

from nz_coder.intelligence.code_index import PersistentCodeIndex
from nz_coder.intelligence.repository_graph import RepositoryGraph
from nz_coder.intelligence.service import RepoIntelligenceService


def _make_repo(root: Path, count: int) -> None:
    for index in range(count):
        previous = index - 1
        imported = (
            f"from module_{previous:05d} import symbol_{previous:05d}\n"
            if previous >= 0 else ""
        )
        call = f"symbol_{previous:05d}()" if previous >= 0 else "0"
        (root / f"module_{index:05d}.py").write_text(
            imported + f"def symbol_{index:05d}(): return {call}\n",
            encoding="utf-8",
        )


def _milliseconds(callback):
    started = time.perf_counter()
    value = callback()
    return round((time.perf_counter() - started) * 1000, 3), value


def benchmark_size(root: Path, count: int) -> dict:
    workspace = root / f"repo-{count}"
    workspace.mkdir(parents=True, exist_ok=True)
    _make_repo(workspace, count)
    index = PersistentCodeIndex(workspace)
    cold_ms, (_entries, cold) = _milliseconds(
        lambda: index.scan(workspace, max_files=count + 10)
    )
    graph = RepositoryGraph(workspace, index=index)
    graph.build(snapshot=index.snapshot())
    warm_ms, (_entries, warm) = _milliseconds(
        lambda: index.scan(workspace, max_files=count + 10)
    )

    target_index = count // 2
    previous_index = target_index - 1
    target = workspace / f"module_{target_index:05d}.py"
    target.write_text(
        f"from module_{previous_index:05d} import symbol_{previous_index:05d}\n"
        f"def symbol_{target_index:05d}(): return symbol_{previous_index:05d}() + 999\n",
        encoding="utf-8",
    )
    edit_ms, edit = _milliseconds(
        lambda: index.update_paths([target.name])
    )
    graph_edit_ms, graph_edit = _milliseconds(
        lambda: graph.update_paths([target.name], snapshot=index.snapshot([target.name]))
    )

    burst_paths = []
    for index_number in range(min(10, count)):
        path = workspace / f"module_{index_number:05d}.py"
        path.write_text(
            f"def symbol_{index_number:05d}(): return {index_number + 1}\n",
            encoding="utf-8",
        )
        burst_paths.append(path.name)
    burst_ms, burst = _milliseconds(lambda: index.update_paths(burst_paths))
    graph_burst_ms, graph_burst = _milliseconds(
        lambda: graph.update_paths(burst_paths, snapshot=index.snapshot(burst_paths))
    )

    symbol_ms, symbol = _milliseconds(
        lambda: index.symbol_context(f"symbol_{target_index:05d}")
    )
    process_ms, process = _milliseconds(
        lambda: index.process_context(
            f"symbol_{target_index:05d}", max_depth=4, limit=100,
            time_budget_ms=100,
        )
    )
    changed_ms, changed = _milliseconds(
        lambda: graph.changed_scope(
            changed_paths=[target.name], max_depth=4, node_limit=100,
            time_budget_ms=100,
        )
    )
    service = RepoIntelligenceService(workspace)
    service.prewarm(max_files=count + 10).result()
    lookup_ms, lookup = _milliseconds(
        lambda: service.intent_lookup(f"symbol {target_index:05d}", limit=20)
    )
    cached_lookup_ms, cached_lookup = _milliseconds(
        lambda: service.intent_lookup(f"symbol {target_index:05d}", limit=20)
    )
    service_metrics = service.metrics()
    service.close()
    return {
        "files": count, "cold_build_ms": cold_ms, "warm_startup_ms": warm_ms,
        "cold_indexed": cold.indexed, "warm_reused": warm.reused,
        "single_file_index_ms": edit_ms, "single_file_graph_ms": graph_edit_ms,
        "single_file_indexed": edit.indexed, "single_file_graph_indexed": graph_edit.indexed,
        "single_file_calls_resolved": edit.calls_resolved,
        "single_file_relationships_updated": graph_edit.relationships_updated,
        "ten_file_burst_index_ms": burst_ms, "ten_file_burst_graph_ms": graph_burst_ms,
        "ten_file_burst_indexed": burst.indexed,
        "ten_file_calls_resolved": burst.calls_resolved,
        "ten_file_relationships_updated": graph_burst.relationships_updated,
        "symbol_query_ms": symbol_ms, "symbol_found": symbol["definition"] is not None,
        "process_query_ms": process_ms, "process_steps": len(process["steps"]),
        "changed_scope_ms": changed_ms,
        "changed_scope_callers": len(changed["transitive_callers"]),
        "structural_lookup_ms": lookup_ms,
        "structural_lookup_results": len(lookup["items"]),
        "cached_structural_lookup_ms": cached_lookup_ms,
        "cached_structural_lookup_results": len(cached_lookup["items"]),
        "structural_lookup_cache_hits": service_metrics["cache_hit"],
        "generation": index.generation(),
    }


def run(output: Path, sizes: tuple[int, ...] = (500, 2000, 5000)) -> dict:
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nz-repo-v3-fixtures-") as directory:
        fixture_root = Path(directory)
        result = {
            "benchmark": "repo-intelligence-v3",
            "sizes": [benchmark_size(fixture_root, size) for size in sizes],
        }
    (output / "repo-intelligence-performance.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--sizes", default="500,2000,5000")
    args = parser.parse_args()
    sizes = tuple(int(value) for value in args.sizes.split(",") if value.strip())
    if args.output:
        result = run(Path(args.output), sizes)
    else:
        with tempfile.TemporaryDirectory(prefix="nz-repo-v3-") as directory:
            result = run(Path(directory), sizes)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
