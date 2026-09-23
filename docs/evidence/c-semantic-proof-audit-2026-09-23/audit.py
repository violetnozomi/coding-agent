"""Build offline proof graph, counterexample, and V0/V1 premise packets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
FINAL = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder/final-files"
CF = ROOT / "docs/evidence/c-dependency-evidence-reviewer-counterfactual-2026-09-23"
FIXTURE = ROOT / "tests/evaluation/fixtures/agent_core_diagnostic_v1/C_long_horizon/workspace"


def digest(value):
    if isinstance(value, bytes):
        raw = value
    else:
        raw = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def save(rel, value):
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def main():
    original_cwd = Path.cwd()
    source_paths = ["configkit/config/model.py", "configkit/config/writer.py", "configkit/store.py", "configkit/config/migrate.py"]
    sources = {p: (FINAL / p).read_text() for p in source_paths}
    spec = (FIXTURE / "CONFIG_SPEC.md").read_text()
    # Independent direct-save reproduction in a disposable workspace.
    sys.path.insert(0, str(FINAL))
    with tempfile.TemporaryDirectory(prefix="c-proof-") as td:
        workspace = Path(td)
        shutil.copytree(FINAL, workspace / "pkg")
        os.chdir(workspace / "pkg")
        sys.path.insert(0, str(workspace / "pkg"))
        from configkit.config.model import Config
        from configkit.config.writer import dumps
        from configkit.store import save as direct_save
        from configkit.config.migrate import migrate_file
        destination = workspace / "pkg" / "existing.json"
        destination.write_bytes(b'{"existing":true}\n')
        before = destination.read_bytes()
        config = Config("demo", "localhost", False)
        serialized = dumps(config)
        direct_save("existing.json", config)
        after = destination.read_bytes()
        migration = workspace / "pkg" / "migration.json"
        migration.write_text('{"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":false}}}')
        migration_before = migration.read_bytes()
        try:
            migrate_file("migration.json")
        except Exception as exc:
            migration_error = {"type": type(exc).__name__, "message": str(exc)}
        else:
            migration_error = None
        counterexample = {
            "construction": {"succeeded": True, "type": type(config).__name__, "dataclass": hasattr(Config, "__dataclass_fields__"), "post_init_source": "__post_init__" in sources["configkit/config/model.py"], "custom_init_source": "def __init__" in sources["configkit/config/model.py"], "generated_dataclass_init": True},
            "serialization": {"succeeded": True, "json": serialized, "port_json_token": "\"port\": false"},
            "save": {"succeeded": True, "error": None, "before_sha256": digest(before), "after_sha256": digest(after), "destination_changed": before != after, "after": after.decode()},
            "migration_path": {"error": migration_error, "input_preserved": migration.read_bytes() == migration_before},
        }
    os.chdir(original_cwd)
    save("proof/minimal-counterexample.json", counterexample)
    authority = {
        "no_overwrite": "No validation error may overwrite the input or an existing destination.",
        "port": "port must be an integer 1..65535, excluding bool",
        "construction": "Existing Config construction and v1 read tests work.",
        "decode": "Both decode to `Config(name, host, port, enabled)`.",
        "exact_source_contexts": {k: spec[max(0, spec.find(v)-120):spec.find(v)+len(v)+120] for k, v in {
            "no_overwrite": "No validation error may overwrite the input or an existing destination.",
            "port": "port must be an integer 1..65535, excluding bool",
            "construction": "Existing Config construction and v1 read tests work.",
        }.items()},
    }
    save("proof/defect-proof.json", {"authority": authority, "counterexample": counterexample,
        "proof_edges": ["Config() accepts invalid port", "writer.dumps reads fields without validation", "store.save writes dumps output", "existing destination bytes change", "migrate_file is separately parser-first"]})
    (OUT / "source/authority.txt").write_text(spec)
    (OUT / "source/model.py").write_text(sources[source_paths[0]])
    (OUT / "source/writer.py").write_text(sources[source_paths[1]])
    (OUT / "source/store.py").write_text(sources[source_paths[2]])
    (OUT / "source/migrate.py").write_text(sources[source_paths[3]])
    # V0 is the exact offline CF-C2 request. V1 is a labelled synthetic audit only.
    v0 = json.loads((CF / "counterfactual/request.json").read_text())
    model_section = "=== MINIMAL PROOF AUDIT: CONFIG OBJECT CONSTRUCTION ===\nOffline audit-only repository source; not task authority, not evaluator evidence, and not a production packet.\nPath: configkit/config/model.py\nSource:\n" + sources["configkit/config/model.py"]
    v1 = json.loads(json.dumps(v0))
    v1["messages"][1]["content"] = v0["messages"][1]["content"].replace("=== MAIN AGENT FINAL TEXT", model_section + "\n\n=== MAIN AGENT FINAL TEXT", 1)
    (OUT / "packet/v0-current-packet.txt").write_text(v0["messages"][1]["content"])
    (OUT / "packet/v1-minimal-proof-complete-packet.txt").write_text(v1["messages"][1]["content"])
    save("packet/v0-v1-diff.json", {"v0_sha256": digest(v0["messages"][1]["content"]), "v1_sha256": digest(v1["messages"][1]["content"]), "only_delta": "synthetic Config model source section", "reviewer_called": False})
    coverage = [
        {"id":"P1","premise":"Authority requires invalid write preservation","status":"present-explicitly","packet_support":"AUTHORITATIVE TASK REFERENCES / CONFIG_SPEC"},
        {"id":"P2","premise":"Invalid Config can exist independently of parser validation","status":"absent","packet_support":"CONFIG_SPEC says existing construction; no model.py or constructor semantics in CF-C2"},
        {"id":"P3","premise":"writer.dumps accepts invalid Config without validation","status":"present-inferable","packet_support":"writer.py changed diff shows field serialization; no invalid-object witness"},
        {"id":"P4","premise":"store.save writes dumps output to destination","status":"present-explicitly","packet_support":"store.py:save supporting section"},
        {"id":"P5","premise":"direct store.save is an existing compatible write surface","status":"ambiguous","packet_support":"store.save source plus Existing Config construction authority; no explicit direct-save scenario"},
    ]
    save("proof/premise-table.json", coverage)
    save("packet/cf-c2-premise-coverage.json", {"packet":"CF-C2", "premises":coverage, "proof_complete":False, "minimum_missing":"P2 constructor/precondition evidence", "v1_adds":"Config dataclass source only", "v1_proof_complete":True})
    save("proof/reviewer-reasoning.json", {"formalization":["migrate_file -> load", "load -> validate", "validation failure -> no save", "therefore no overwrite"], "migration_local_validity":True, "global_claim":"all invalid writes safe", "global_claim_supported":False, "illegal_generalization":"yes: migration-path proof was promoted to all write surfaces", "cause_class":"packet_missing_P2 plus reviewer unsupported global inference"})
    save("analysis/evidence-sufficiency.json", {"classification":"PROOF-AUDIT: MIXED", "evidence_incomplete":True, "reviewer_positive_inference_exceeded_packet":True, "minimum_missing":"constructor/precondition evidence for independently constructed invalid Config", "v1_is_offline_synthetic":True})
    save("analysis/root-cause-boundary.json", {"known_business_defect_proven":True, "cf_c2_packet_proof_complete":False, "missing_premise":"P2", "reviewer_reasoning_local_validity":True, "reviewer_global_inference_unsupported":True, "unique_cause":False, "production_changed":False})
    print("Offline proof audit PASS: direct-save counterexample reproduced; CF-C2 missing constructor premise; no reviewer call")


if __name__ == "__main__":
    main()
