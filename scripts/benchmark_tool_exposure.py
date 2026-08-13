#!/usr/bin/env python3
"""Provider-free 20/50/100/200-tool schema and lexical exposure benchmark."""
from __future__ import annotations

import argparse
import json
import statistics
import time


DOMAINS = (
    "file", "symbol", "reference", "shell", "git", "test", "lsp", "memory",
    "session", "workflow", "mcp", "http", "patch", "search", "diagnostic",
)
ACTIONS = ("read", "write", "find", "list", "inspect", "run", "resolve", "update")


def build_tools(count: int) -> list[dict]:
    tools = []
    for index in range(count):
        domain = DOMAINS[index % len(DOMAINS)]
        action = ACTIONS[(index // len(DOMAINS)) % len(ACTIONS)]
        name = f"{action}_{domain}_{index:03d}"
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"{action} {domain} resources for coding task {index}",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        })
    return tools


def rank(query: str, tools: list[dict], limit: int = 8) -> list[str]:
    terms = set(query.lower().replace("_", " ").split())
    scored = []
    for tool in tools:
        function = tool["function"]
        text = f"{function['name']} {function['description']}".lower().replace("_", " ")
        score = sum(3 if term in function["name"] else 1 for term in terms if term in text)
        scored.append((score, function["name"]))
    return [name for score, name in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]]


def benchmark(count: int, iterations: int) -> dict:
    tools = build_tools(count)
    queries = []
    for tool in tools[: min(20, count)]:
        name = tool["function"]["name"]
        action, domain, _index = name.split("_")
        queries.append((f"{action} {domain}", name))
    latencies = []
    hits = 0
    for _ in range(iterations):
        started = time.perf_counter_ns()
        for query, expected in queries:
            hits += expected in rank(query, tools)
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
    encoded = json.dumps(tools, separators=(",", ":"), ensure_ascii=False)
    return {
        "tools": count,
        "schema_chars": len(encoded),
        "coarse_schema_tokens": (len(encoded) + 3) // 4,
        "top8_recall": hits / (iterations * len(queries)),
        "median_batch_latency_ms": statistics.median(latencies),
        "p95_batch_latency_ms": sorted(latencies)[max(0, int(len(latencies) * .95) - 1)],
        "queries_per_batch": len(queries),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = [
        benchmark(count, max(1, args.iterations))
        for count in (20, 50, 100, 200)
    ]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("tools schema_chars coarse_tokens recall@8 median_ms p95_ms")
        for row in results:
            print(
                f"{row['tools']:>5} {row['schema_chars']:>12} "
                f"{row['coarse_schema_tokens']:>13} {row['top8_recall']:.3f} "
                f"{row['median_batch_latency_ms']:.3f} {row['p95_batch_latency_ms']:.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
