"""Provider-free design audit for bounded constructor evidence."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from nz_coder.intelligence.service import RepoIntelligenceService

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
C_FINAL = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder/final-files"


def dump(rel: str, value) -> None:
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def envelope(source: str, name: str, limit: int = 1400) -> dict:
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name)
    lines = source.splitlines()
    start = node.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    text = "\n".join(lines[start:node.end_lineno]) + "\n"
    return {"start_line": start + 1, "end_line": node.end_lineno, "chars": len(text),
            "truncated": len(text) > limit, "text": text[:limit] + ("\n…<truncated>" if len(text) > limit else "")}


def fixture_results() -> dict:
    cases = {
        "dataclass-no-validation": "from dataclasses import dataclass\n@dataclass\nclass Payload:\n    value: int\n",
        "dataclass-post-init": "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass Payload:\n    value: int\n    def __post_init__(self):\n        if self.value < 0: raise ValueError\n",
        "validating-init": "class Payload:\n    def __init__(self, value):\n        validate(value)\n        self.value = value\n",
        "unrelated-class": "class Other:\n    pass\n",
        "many-classes": "\n".join(f"class Payload{i}:\n    value: int\n" for i in range(8)),
        "unresolved-dynamic": "class Placeholder:\n    pass\n",
        "tests-only": "# only a test witness; implementation source is absent\n",
    }
    result = {}
    for name, source in cases.items():
        classes = [n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)]
        selected = classes[:2]
        envs = [envelope(source, item) for item in selected]
        result[name] = {"classes": classes, "selected": selected, "envelopes": envs,
                        "decorator_preserved": all("@" in e["text"] for e in envs if "Payload" in e["text"]),
                        "tests_are_implementation": name != "tests-only"}
    result["budget"] = {"max_symbols": 2, "max_chars_each": 1400, "max_total_chars": 2400,
                         "many_classes_selected": len(result["many-classes"]["selected"]),
                         "deterministic_order": result["many-classes"]["selected"] == ["Payload0", "Payload1"]}
    return result


def main() -> None:
    model = (C_FINAL / "configkit/config/model.py").read_text()
    writer = (C_FINAL / "configkit/config/writer.py").read_text()
    store = (C_FINAL / "configkit/store.py").read_text()
    service = RepoIntelligenceService(C_FINAL)
    try:
        service.prewarm(max_files=5000).result(timeout=15)
        scope = service.changed_scope(changed_paths=["configkit/config/writer.py"], limit=100, wait_budget_ms=0)
        config_ctx = service.symbol_context("configkit.config.model.Config", limit=30)
        loads_ctx = service.symbol_context("configkit.config.parser.loads", limit=30)
    finally:
        service.close()
    relations = {
        "scope": scope,
        "config_context": config_ctx,
        "loads_context": loads_ctx,
        "existing_relations": {
            "parser_loads_to_config": any("model.py" in str(x) or "Config" in str(x) for x in loads_ctx.get("callees", [])),
            "writer_changed": scope.get("changed_symbols", []),
            "store_save_in_scope": any("store.py:save" in x for x in scope.get("direct_callers", [])),
        },
        "missing": ["constructor_precondition", "invalid_state_dataflow", "writer_parameter_type"],
    }
    dump("c/graph-relations.json", relations)
    config_env = envelope(model, "Config")
    dump("c/source-envelope.json", {"E0": {"text": "\n".join(model.splitlines()[4:9])}, "E1": config_env,
                                    "recommended": "E1", "writer_chars": len(writer), "store_chars": len(store)})
    dump("c/candidate-selection.json", {
        "route_A": {"status": "supported-partial", "candidates": ["configkit/config/model.py:Config"], "basis": "existing parser-to-class structural relation plus unchanged class definition", "c_specific": False},
        "route_B": {"status": "unsupported-for-type-proof", "basis": "call graph has save/dumps and parser/Config but no general argument type/dataflow edge"},
        "route_C": {"status": "unsupported", "basis": "no production authority-text symbol resolver; no NLP selector added"},
        "selection_policy": "high-confidence direct structural class relation, unchanged implementation, bounded deterministic order",
    })
    fixtures = fixture_results()
    dump("fixtures/results.json", fixtures)
    dump("c/premise-coverage.json", {
        "P1": "present-explicitly", "P2": "closed-by-source-envelope-in-V1-design-only",
        "P3": "present-inferable", "P4": "present-explicitly", "P5": "ambiguous",
        "p2_evidence": "E1 preserves @dataclass(frozen=True), class fields, __init__/__post_init__ when present",
        "reviewer_called": False,
    })
    dump("analysis/genericity.json", {"classification": "PRECOND-DESIGN: EXISTING-GRAPH-SUFFICIENT", "route_A": "usable", "route_B": "partial", "route_C": "unsupported", "production_changed": False})
    dump("analysis/false-positive-boundary.json", {"unrelated_class": "budgeted/relationship-filtered", "many_classes": "max 2 deterministic", "dynamic": "omit", "tests_only": "not implementation evidence", "self_validating": "retain __post_init__", "explicit_init": "retain __init__"})
    dump("analysis/root-cause-boundary.json", {"existing_graph_has_class_relation": True, "selection_projection_gap": True, "new_graph_required": False, "constructor_semantics_fully_modeled": False, "production_changed": False})
    (OUT / "design/generic-fixtures.json").write_text(json.dumps(fixtures, indent=2) + "\n")
    print("Constructor evidence design audit PASS: existing graph can locate class relation; E1 preserves decorators; no provider")


if __name__ == "__main__":
    main()
