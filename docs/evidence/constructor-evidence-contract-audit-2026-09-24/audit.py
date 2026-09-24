"""Offline contract experiment. Not a production collector and never a packet writer.

Observes existing PythonAstAnalyzer SymbolRecord emission at its original AST node;
proposed span metadata lives only in this script. Production roles and targets are
read from the real persistent index. Source is neither imported nor executed.
"""

# ruff: noqa: E402 -- install offline guards before production imports
from __future__ import annotations

import ast
from contextlib import contextmanager
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import tempfile
import sys
from types import SimpleNamespace
from unittest.mock import patch


# Local experiment hard guard: no socket transport, provider or embedding imports.
class OfflineOnly:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nz_coder.providers" or fullname.startswith(
            ("nz_coder.providers.", "openai", "sentence_transformers", "infcodex")
        ):
            raise RuntimeError("Forbidden online/model module: " + fullname)
        return None


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Offline audit forbids network transport")


sys.meta_path.insert(0, OfflineOnly())
sys.addaudithook(deny_network)

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.intelligence import analyzers
from nz_coder.intelligence.code_index import ResolvedCallLocation
from nz_coder.intelligence.service import RepoIntelligenceService
from nz_coder.runtime.verification.dependency_evidence import (
    _safe_source,
    MAX_FILE_BYTES,
)

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "8702e10aa52638a154f8466c10d478c30a576d38"
LABEL = (
    "OFFLINE SYNTHETIC CONTRACT VIEW — supporting repository source only.\n"
    "Not task authority, permission, instructions, execution proof, or a claim of semantic relevance.\n"
)
MAX_SYMBOLS, MAX_EACH, MAX_TOTAL = 2, 1400, 2400


def sha(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def save(path, obj):
    (OUT / path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


@contextmanager
def proposed_spans():
    """Observe production AST once, not a downstream parse or second symbol locator."""
    original_record = analyzers.SymbolRecord
    original_parse = ast.parse
    spans, parses = {}, []

    def parse(*args, **kwargs):
        parses.append(str(kwargs.get("filename", "unknown")))
        return original_parse(*args, **kwargs)

    def record(*args, **kwargs):
        frame = inspect.currentframe().f_back
        result = original_record(*args, **kwargs)
        node = frame.f_locals.get("node")
        if frame.f_code.co_name == "collect" and isinstance(node, ast.ClassDef):
            spans[result.symbol_id] = {
                "class_line": node.lineno,
                "end_line": node.end_lineno,
                "proposed_start_line": min(
                    [node.lineno] + [d.lineno for d in node.decorator_list]
                ),
                "decorators": [
                    {"line": d.lineno, "end_line": d.end_lineno, "column": d.col_offset}
                    for d in node.decorator_list
                ],
                "persisted": False,
                "origin": "original-python-ast-symbol-emission",
            }
        return result

    with (
        patch.object(analyzers, "SymbolRecord", record),
        patch.object(ast, "parse", parse),
    ):
        yield spans, parses


def build(workspace):
    service = RepoIntelligenceService(workspace)
    with proposed_spans() as (spans, parses):
        service.prewarm(max_files=300).result(timeout=15)
    assert service.state.status == "ready"
    # Fresh fixture/disposable workspace: exactly one parse for each native file.
    snapshot = service.index.snapshot()
    native_files = [
        f for f in snapshot.files if f.language == "python" and f.source == "python-ast"
    ]
    assert len(parses) == len(native_files), (len(parses), len(native_files))
    return service, spans


def trusted_role(edge, snapshot, workspace):
    """P-A: same-snapshot caller provenance, not target-resolution edge.source."""
    entries = {f.path: f for f in snapshot.files}
    entry = entries.get(edge.path)
    if not entry or entry.parse_error or entry.language != "python":
        return False
    if entry.source != "python-ast" or entry.capability_tier != "ast-native":
        return False
    owner = next(
        (s for s in entry.symbols if s.symbol_id == edge.caller_symbol_id), None
    )
    if (
        not owner
        or owner.source != "python-ast"
        or owner.capability_tier != "ast-native"
    ):
        return False
    stat = WorkspaceFileAccess(workspace).stat(edge.path)
    return (
        stat.mtime_ns,
        stat.size,
    ) == entry.fingerprint and edge.usage_role != "unknown"


def candidate_rows(service, changed, active=True):
    snapshot = service.index.snapshot()
    if not active or service.state.status != "ready":
        return []
    symbols = {s.symbol_id: s for f in snapshot.files for s in f.symbols}
    scope = service.changed_scope(
        changed_paths=changed,
        limit=100,
        max_depth=1,
        node_limit=100,
        time_budget_ms=100,
        wait_budget_ms=0,
    )
    assert (
        scope["freshness"] == "indexed" and scope["generation"] == snapshot.generation
    )
    origins = set(scope["changed_symbol_ids"])
    unique = {}
    for edge in snapshot.calls:
        target = symbols.get(edge.callee_symbol_id)
        if (
            edge.caller_symbol_id not in origins
            or not _safe_source(edge.path, set())
            or not target
            or target.kind != "class"
            or edge.confidence < 0.85
            or target.confidence < 0.85
            or target.language != "python"
            or target.source != "python-ast"
            or target.capability_tier != "ast-native"
            or edge.usage_role != "returned"
            or not trusted_role(edge, snapshot, service.workspace)
            or not _safe_source(target.file_path, set(changed))
        ):
            continue
        row = {
            "path": target.file_path,
            "symbol_id": target.symbol_id,
            "symbol": target.name,
            "edge": dataclasses.asdict(edge),
            "definition": dataclasses.asdict(target),
            "role_provenance": {
                "source": "python-ast",
                "capability": "ast-native",
                "basis": "same-snapshot caller file and symbol",
            },
            "generation": snapshot.generation,
        }
        unique.setdefault(target.symbol_id, row)
    return sorted(unique.values(), key=lambda r: (r["path"], r["symbol_id"]))


def envelope(service, row, spans):
    """Render safe current bytes from proposed AST span; never reconstruct spans."""
    snapshot = service.index.snapshot([row["path"]])
    entry = snapshot.files[0]
    source, identity = WorkspaceFileAccess(service.workspace).read_text_with_identity(
        row["path"], maximum=MAX_FILE_BYTES
    )
    assert snapshot.generation == row["generation"] == service.state.generation
    if (identity.mtime_ns, identity.size) != entry.fingerprint:
        return {"included": False, "reason": "stale_source"}
    if "\x00" in source:
        return {"included": False, "reason": "nontext"}
    span = spans.get(row["symbol_id"])
    if not span:
        return {"included": False, "reason": "source_span_unavailable"}
    lines = source.splitlines(keepends=True)
    # Exact local prefix validation at an AST coordinate; no scanning or regex.
    # Parenthesized decorator expressions can lose '@(' positions in Python AST.
    for d in span["decorators"]:
        prefix = lines[d["line"] - 1].encode()[: d["column"]]
        if not prefix.endswith(b"@") or prefix[:-1].strip():
            return {
                "included": False,
                "reason": "decorator_start_unproven",
                "span": span,
            }
    start, end = span["proposed_start_line"], span["end_line"]
    assert 1 <= start <= row["definition"]["line"] <= end <= len(lines)
    text = "".join(lines[start - 1 : end])
    header = (
        f"Path: {row['path']}\nSymbol: {row['symbol_id']}\n"
        f"Relation: direct_returned_class; role provenance: python-ast/ast-native\n"
        f"Unchanged: true; lines: {start}-{end}\nSource:\n"
    )
    return {
        "included": True,
        "reason": "complete_source_span",
        "text": text,
        "block": header + text,
        "span": span,
        "full_source_hash": identity.content_hash,
        "envelope_hash": sha(text),
        "rendered_chars": len(header + text),
    }


def allocate(service, rows, spans, max_symbols=MAX_SYMBOLS):
    selected, decisions = [], []
    total = len(LABEL) + 100  # reserve fixed diagnostic footer space
    for row in rows:
        view = envelope(service, row, spans)
        reason = view["reason"]
        if view["included"]:
            if view["rendered_chars"] > MAX_EACH:
                reason = "envelope_exceeds_budget"
            elif len(selected) >= max_symbols:
                reason = "symbol_budget"
            elif total + view["rendered_chars"] > MAX_TOTAL:
                reason = "total_budget"
            else:
                selected.append({**row, "envelope": view})
                total += view["rendered_chars"]
        decisions.append({"symbol_id": row["symbol_id"], "reason": reason})
    text = LABEL + "\n".join(x["envelope"]["block"] for x in selected)
    text += f"\nOmitted candidate symbols: {len(rows) - len(selected)}\n"
    assert len(text) <= MAX_TOTAL and len(selected) <= max_symbols
    return {
        "selected": selected,
        "decisions": decisions,
        "rendered_chars": len(text),
        "text": text,
        "omitted_count": len(rows) - len(selected),
    }


MODEL = (
    "from dataclasses import dataclass\n@dataclass\nclass Payload:\n    value: int = 0\n"
    "class TelemetryMarker:\n    pass\nclass AuditMarker:\n    pass\nclass Wrapper:\n    pass\n"
)
CASES = {
    "returned-payload": ("return Payload()", ["Payload"]),
    "returned-telemetry": ("return TelemetryMarker()", ["TelemetryMarker"]),
    "discarded-marker": ("AuditMarker()\n    return Payload()", ["Payload"]),
    "mixed-returned": (
        "if flag:\n        return Payload()\n    return TelemetryMarker()",
        ["Payload", "TelemetryMarker"],
    ),
    "wrapped-constructor": ("return Wrapper(Payload())", ["Wrapper"]),
    "assigned-returned": ("obj = Payload()\n    return obj", []),
}


def relevance():
    for name, (body, expected) in CASES.items():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "records.py").write_text(MODEL)
            (root / "changed.py").write_text(
                "from records import Payload, TelemetryMarker, AuditMarker, Wrapper\ndef changed(flag=False):\n    "
                + body
                + "\n"
            )
            service, spans = build(root)
            try:
                rows = candidate_rows(service, ["changed.py"])
                assert [r["symbol"] for r in rows] == expected
                assert candidate_rows(service, ["changed.py"], active=False) == []
                result = allocate(service, rows, spans)
                assert [r["symbol"] for r in result["selected"]] == expected
                assert result == allocate(
                    service, candidate_rows(service, ["changed.py"]), spans
                )
                save(
                    "relevance/" + name + ".json",
                    {
                        "fixture": (root / "changed.py").read_text(),
                        "candidates": rows,
                        "budget": result,
                        "semantic_relevance_claimed": False,
                    },
                )
            finally:
                service.close()


ENVELOPES = {
    "single-decorator": "@dataclass(frozen=True)\nclass Payload:\n    value: int\n",
    "multiline-decorator": "@decorator(\n    validate=True,\n    frozen=True,\n)\nclass Payload:\n    value: int\n",
    "multi-decorator": "@first\n@second(\n    validate=True,\n)\nclass Payload:\n    value: int\n",
    "post-init": "@dataclass\nclass Payload:\n    value: int\n    def __post_init__(self):\n        if self.value < 0:\n            raise ValueError\n",
    "explicit-init": "class Payload:\n    def __init__(self, value):\n        validate(value)\n        self.value = value\n",
    "class-validator": "class Payload:\n    @classmethod\n    def validate(cls, value):\n        if value < 0: raise ValueError\n",
    "inherited": "class Payload(Base):\n    value: int\n",
    "nested": "class Outer:\n    @dataclass\n    class Payload:\n        value: int\n",
    "parenthesized-decorator": "@(\n    decorator\n)\nclass Payload:\n    value: int\n",
    "large-class": "@dataclass\nclass Payload:\n    value: int\n"
    + "    # padding\n" * 130
    + "    def __post_init__(self):\n        raise ValueError\n",
}


def envelopes():
    for name, source in ENVELOPES.items():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "records.py").write_text(source)
            service, spans = build(root)
            try:
                # Envelope-only tests resolve real identity, not selection claims.
                definition = service.symbol_context("Payload", wait_budget_ms=0)[
                    "definition"
                ]
                row = {
                    "path": definition["path"],
                    "symbol_id": definition["symbol_id"],
                    "generation": service.state.generation,
                    "definition": definition,
                }
                view = envelope(service, row, spans)
                if name == "parenthesized-decorator":
                    assert (
                        not view["included"]
                        and view["reason"] == "decorator_start_unproven"
                    )
                else:
                    assert view["included"]
                    expected = (
                        source
                        if name != "nested"
                        else "".join(source.splitlines(keepends=True)[1:])
                    )
                    assert view["text"] == expected
                    if name == "large-class":
                        assert "__post_init__" not in view["text"][:1400]
                        result = allocate(service, [row], spans)
                        assert (
                            not result["selected"]
                            and result["decisions"][0]["reason"]
                            == "envelope_exceeds_budget"
                        )
                        view["allocation"] = result
                save(
                    "envelope/" + name + ".json",
                    {"source": source, "definition": definition, "view": view},
                )
            finally:
                service.close()


def provenance():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text(
            "def changed(factory):\n    return factory()\ndef unknown(factory):\n    return [factory()]\n"
        )
        (root / "target.py").write_text("class Resolved:\n    pass\n")
        service, _ = build(root)
        try:
            before = service.index.snapshot()
            returned = next(e for e in before.calls if e.caller == "changed")
            assert trusted_role(returned, before, root)
            caller = next(f for f in before.files if f.path == "a.py")
            for updates in (
                {"language": "typescript"},
                {"capability_tier": "lexical-fallback"},
                {"parse_error": "invalid syntax"},
                {"source": "lexical"},
            ):
                modified = dataclasses.replace(
                    before,
                    files=tuple(
                        dataclasses.replace(f, **updates)
                        if f.path == caller.path
                        else f
                        for f in before.files
                    ),
                )
                assert not trusted_role(returned, modified, root)
            save(
                "provenance/ast-native.json",
                {"edge": dataclasses.asdict(returned), "trusted": True},
            )
            resolver = SimpleNamespace(
                resolve=lambda r: ResolvedCallLocation("target.py", 1, name="Resolved")
            )
            assert (
                service.index.augment_call_targets(
                    resolver, time_budget_ms=1000
                ).resolved
                == 2
            )
            after = service.index.snapshot()
            returned = next(e for e in after.calls if e.caller == "changed")
            unknown = next(e for e in after.calls if e.caller == "unknown")
            assert (
                returned.source == "lsp-definition"
                and returned.usage_role == "returned"
            )
            assert trusted_role(returned, after, root) and not trusted_role(
                unknown, after, root
            )
            save(
                "provenance/lsp-resolved.json",
                {
                    "returned": dataclasses.asdict(returned),
                    "unknown": dataclasses.asdict(unknown),
                    "caller_file": dataclasses.asdict(
                        next(f for f in after.files if f.path == "a.py")
                    ),
                    "trusted_after_lsp": True,
                    "unknown_not_promoted": True,
                },
            )
            # Stale caller bytes must not inherit old capability/role pairing.
            (root / "a.py").write_text("def changed(factory):\n    factory()\n")
            assert not trusted_role(returned, after, root)
            save(
                "provenance/caller-capability.json",
                {
                    "same_snapshot_required": True,
                    "file_and_owner_python_ast_native": True,
                    "stale_caller_rejected": True,
                    "non_python_lexical_parse_error_wrong_source_rejected": True,
                    "classification": "ROLE-PROVENANCE: CALLER-CAPABILITY-SUFFICIENT",
                    "boundary": "current built-in analyzer, schema v4, unmodified trusted derived cache; no standalone edge inference",
                },
            )
        finally:
            service.close()


def budgets():
    outputs = {}
    for name, count, lines in [
        ("short", 1, 0),
        ("medium", 1, 38),
        ("large", 1, 130),
        ("two", 2, 0),
        ("two-medium", 2, 70),
        ("five", 5, 0),
    ]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            names = [f"Item{i}" for i in range(count)]
            (root / "records.py").write_text(
                "".join(
                    "class "
                    + n
                    + ":\n    value: int\n"
                    + "    # padding\n" * lines
                    + "    tail: int\n"
                    for n in names
                )
            )
            (root / "changed.py").write_text(
                "from records import "
                + ", ".join(names)
                + "\ndef changed(flag):\n"
                + "".join(
                    f"    if flag == {i}:\n        return {n}()\n"
                    for i, n in enumerate(names)
                )
            )
            service, spans = build(root)
            try:
                rows = candidate_rows(service, ["changed.py"])
                one, two = (
                    allocate(service, rows, spans, 1),
                    allocate(service, rows, spans, 2),
                )
                assert rows == sorted(rows, key=lambda r: (r["path"], r["symbol_id"]))
                assert two == allocate(
                    service, candidate_rows(service, ["changed.py"]), spans, 2
                )
                if name == "large":
                    assert not two["selected"]
                if name == "five":
                    assert len(two["selected"]) == 2 and two["omitted_count"] == 3
                if name == "two-medium":
                    assert (
                        len(two["selected"]) == 1
                        and two["decisions"][1]["reason"] == "total_budget"
                    ), [
                        (r["symbol"], envelope(service, r, spans)["rendered_chars"])
                        for r in rows
                    ]
                outputs[name] = {"max_one": one, "max_two": two}
            finally:
                service.close()
    save(
        "analysis/false-positive-budget.json",
        {
            "per_symbol": MAX_EACH,
            "total": MAX_TOTAL,
            "max_symbols": MAX_SYMBOLS,
            "counting": "rendered block headers/source plus section/footer",
            "cases": outputs,
        },
    )


def frozen_c():
    history = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder"
    cf = ROOT / "docs/evidence/c-sidecar-fixed-diff-counterfactual-2026-09-22"
    changed = json.loads(
        (cf / "packets/historical-verifier2-summary.json").read_text()
    )["runtime_state"]["changed_files"]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "workspace"
        shutil.copytree(
            history / "final-files",
            root,
            ignore=shutil.ignore_patterns(".nz-coder", "__pycache__"),
        )
        service, spans = build(root)
        try:
            rows = candidate_rows(service, changed)
            result = allocate(service, rows, spans)
            # C-specific assertions only; selection above never names the fixture.
            item = next(x for x in result["selected"] if x["symbol"] == "Config")
            assert item["edge"]["usage_role"] == "returned"
            assert item["envelope"]["text"].startswith(
                "@dataclass(frozen=True)\nclass Config:"
            )
            assert item["envelope"]["text"].endswith("    enabled: bool = True\n")
            assert all(
                s not in result["text"]
                for s in ("11/12", "invalid_write_preserves_destination", "oracle/")
            )
            save(
                "c/candidates.json",
                {
                    "changed_paths": changed,
                    "candidates": rows,
                    "budget": result,
                    "Sidecar_modified": False,
                },
            )
            save("envelope/frozen-c-config.json", item)
        finally:
            service.close()


def security():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "workspace"
        root.mkdir()
        outside = Path(td) / "outside.py"
        outside.write_text("PRIVATE")
        (root / "link.py").symlink_to(outside)
        access = WorkspaceFileAccess(root)
        rejected = []
        for name in ("../outside.py", "link.py"):
            try:
                access.read_text_with_identity(name, maximum=100)
            except (ValueError, OSError):
                rejected.append(name)
        assert len(rejected) == 2
        assert not _safe_source("oracle/a.py", set()) and not _safe_source(
            "tests/a.py", set()
        )
        (root / "big.py").write_text("x" * 101)
        try:
            access.read_text_with_identity("big.py", maximum=100)
        except ValueError:
            rejected.append("big.py")
        assert "big.py" in rejected
        save(
            "analysis/security-boundary.json",
            {
                "reader": "production WorkspaceFileAccess",
                "rejected": rejected,
                "policy": "existing dependency _safe_source excludes tests/docs/evidence/oracle/etc.",
                "source_execution": False,
                "note": "decorators and validators in envelope fixtures are undefined; parsing succeeds without execution",
            },
        )


def main():
    relevance()
    envelopes()
    provenance()
    budgets()
    frozen_c()
    security()
    save(
        "analysis/test-results.json",
        {
            "relevance_fixtures": 6,
            "envelope_fixtures": 10,
            "budget_cases": 6,
            "LSP_provenance": "pass",
            "frozen_C": "pass",
            "source_safety": "pass",
            "production_changed": False,
        },
    )
    print(
        "PASS: 6 real-graph policy fixtures; 10 AST-envelope cases; 6 budget cases; LSP/current-source trust; frozen C; source safety"
    )


if __name__ == "__main__":
    main()
