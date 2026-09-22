"""Offline C facts/projection audit. Never instantiate a model client."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22"
RUN = OLD / "C/nzcoder"
SUITE = ROOT / "tests/evaluation/fixtures/agent_core_diagnostic_v1"
BASE = "e5cea83009d7ebf0e37991e4cd854c0f68de4a8f"
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(path.read_text())


def clean(text):
    return text.replace(str(ROOT), "<REPO>").replace(str(Path.home()), "<USER_HOME>")


def save(name, value):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(clean(json.dumps(value, indent=2, ensure_ascii=False)) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    from nz_coder.runtime.agent.task_policy import mutation_instruction_scopes, task_wants_tests
    from nz_coder.runtime.verification.sidecar_verifier import (
        _bounded_diff_hints, _rank_semantic_paths, _render_contract_criteria,
        build_verifier_context, build_verifier_user_message,
    )
    state = read(RUN / "final-state.json")["state"]
    reviews = read(RUN / "semantic-review.json")
    spec = (SUITE / "C_long_horizon/workspace/CONFIG_SPEC.md").read_text()
    task = (SUITE / "C_long_horizon/task.md").read_text()
    save("baseline/c-closure-facts.json", {
        "authority_complete": True, "authority_model_observed": True,
        "verifier_saw_full_authority": True, "project_tests_passed": 46,
        "independent_acceptance": "11/12", "failed_behavior": "invalid_write_preserves_destination",
        "runtime_contract_requirements": ["R1", "R2", "R3"], "runtime_terminal": "completed",
        "semantic_final_verdict": "accept", "source": str(RUN.relative_to(ROOT)),
        "note": "historical observations, not altered by the offline projection fix",
    })
    save("baseline/contract.json", state["task_contract"])
    save("baseline/ledger.json", state["requirement_ledger"])
    save("baseline/verifier-input.json", [r["visible_input"] for r in reviews])
    events = [json.loads(line) for line in (RUN / "runtime.jsonl").read_text().splitlines()]
    planning = [e for e in events if "planning" in e.get("event", "") or e.get("event") == "task_contract_bootstrapped"]
    save("contract/planner-vs-fallback.json", {
        "historical_events": planning, "classification": "A1",
        "planner_request_observed": False,
        "origin": "ProductRunEnvironment._maybe_generate_plan -> derive_task_contract -> TaskContract.from_dict -> RuntimeState.set_task_contract",
        "controlled_cases": ["disabled", "unavailable", "coarse", "detailed"],
        "detailed_contract_preserved": True,
        "planner_context_source": "_call_planning_llm uses task, mode, acceptance criteria and exact command; no retained spec body",
        "test_change_requested_by_classifier": task_wants_tests(task),
        "mutation_scopes": mutation_instruction_scopes(task),
        "spec_clauses_extracted": False,
        "reason": "Reference scope contains including ... tests and documentation; it is not a mutation scope. Test-change regex does not recognize that general inclusion phrase. No spec-body clause extraction exists.",
    })
    save("contract/authoritative-reference-flow.json", {
        "capture": "lifecycle.prepare_runtime_state -> bind_task_references -> capture_references",
        "contract": "_maybe_generate_plan reads initial_task_text and verification_contract, not retained text",
        "verifier": "SidecarVerifierHook._evidence -> build_verifier_context -> authoritative references",
        "reference": state["task_reference_evidence"],
    })
    matrix = [read(p) for p in sorted((OUT / "ledger").glob("matrix-*.json"))]
    save("ledger/satisfaction-mode-matrix.json", matrix)
    save("contract/bootstrap-replay.json", {
        p.stem: read(p) for p in sorted((OUT / "contract").glob("bootstrap-*.json"))
        if p.name != "bootstrap-replay.json"
    })
    visibility = []
    for index, review in enumerate(reviews, 1):
        visible = review["visible_input"][1]["content"]
        request = visible.split("=== USER REQUEST (CURRENT TURN) ===", 1)[1].split("=== AUTHORITATIVE", 1)[0].strip()
        assert request == task.strip()
        assert json.dumps(spec, ensure_ascii=False) in visible
        writer_line = next(line for line in visible.splitlines() if line.startswith("- configkit/config/writer.py:"))
        visibility.append({
            "verifier": index, "full_original_spec": True,
            "no_overwrite_clause_visible": "No validation error may overwrite" in visible,
            "current_user_equals_original_task": True,
            "writer_file_section": writer_line,
            "writer_body_somewhere_in_packet": "def to_dict(config):" in visible,
            "unchanged_store_body_present": "def save(" in visible,
            "structured_runtime_requirement_count": 3,
            "clause_inventory_present": False,
            "full_ledger_status_evidence_list_projected": False,
            "projection": "Requirement descriptions/required evidence and exact verification output; not the complete ledger object",
            "odd_previous_output_phrase_in_input": "Compared with my previous output" in visible,
            "synthetic_role_labels": visible.count("SYNTHETIC REVIEW/CONTROL GUIDANCE; NOT USER AUTHORITY"),
            "diff_omissions": [line for line in visible.splitlines() if "diff evidence omitted" in line],
        })
    save("verifier/context-audit.json", visibility)
    save("verifier/role-audit.json", {
        "first_verifier_input_contains_odd_reason": False,
        "first_verifier_genuine_query": task,
        "host_role_separation": "system explicitly third-party; main text/tools labeled; synthetic messages not genuine query",
        "second_verifier_odd_phrase_source": "previous reviewer feedback, correctly labeled synthetic",
        "changed_runtime_fields": [k for k in reviews[0]["runtime_before"]
                                   if reviews[0]["runtime_before"].get(k) != reviews[1]["runtime_before"].get(k)],
        "changed_context": ["final assistant report", "recent rolling transcript including synthetic review feedback"],
        "unchanged": ["source bytes", "mutation generation", "verification", "retained authority"],
        "causal_limit": "Cannot infer internal model cause or attribute verdict difference exclusively to narration",
    })
    save("verifier/obligation-visibility.json", {
        "authority": "complete", "typed_inventory": "R1/R2/R3 only",
        "fine_grained_inventory": None,
        "evaluator_ledger": "not injected", "diff": "bounded and incorrectly attributed on baseline",
    })
    mapping = {}
    for obligation in read(RUN / "user-obligations-final.json")["obligations"]:
        kind = obligation["kind"]
        ids = ["R3"] if kind == "verification" else ["R1", "R2"] if kind == "compatibility" else ["R1"]
        mapping[obligation["id"]] = {
            "authority_source": obligation["source"], "quote": obligation["quote"],
            "runtime_requirement_ids": ids, "mapping_scope": "retrospective broad semantic coverage, not a host-extracted clause",
            "deterministic_evidence": ["verification_passed"],
            "semantic_evidence": ["semantic_review_passed"] if "R2" in ids else [],
            "independent_checks": obligation["acceptance_checks"],
            "evaluator_status": obligation["status"], "runtime_final_status": "satisfied",
        }
    save("analysis/obligation-runtime-map.json", mapping)

    module_spec = importlib.util.spec_from_file_location("diagnostic_validate", SUITE / "validate.py")
    validator = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(validator)
    with tempfile.TemporaryDirectory(prefix="c-closure-bad-workspace-") as temporary:
        workspace = Path(temporary) / "workspace"
        hashes = validator.snapshot(RUN / "final-files", workspace)
        project = validator.project_tests("C_long_horizon", workspace)
        accepted = validator.acceptance("C_long_horizon", workspace)
        assert project["exit"] == 0 and "46 passed" in project["stdout"]
        assert accepted["passed"] == 11 and accepted["total"] == 12
        assert [c["name"] for c in accepted["checks"] if not c["passed"]] == ["invalid_write_preserves_destination"]
        assert validator.hashes(workspace) == hashes
        save("verifier/final-bad-workspace-hashes.json", hashes)
        save("verifier/bad-workspace-project-tests.json", project)
        save("verifier/bad-workspace-acceptance.json", accepted)

    # A corrected host projection, NOT a new model verdict or historical input.
    diff = (RUN / "workspace.diff").read_text()
    paths = _rank_semantic_paths(state["changed_files"], state)
    hints = _bounded_diff_hints(diff, paths, max_each=5000, max_total=12000)
    packet = build_verifier_context([{"role": "user", "content": task}],
        "Offline projection audit; no model call.",
        file_edits=[{"path": p, "diff_hint": hints.get(p, "(budget omitted)")} for p in paths],
        authoritative_references=state["task_reference_evidence"],
        additional_criteria="\n".join(_render_contract_criteria(state)))
    save("verifier/corrected-projection.json", {
        "scope": "offline host projection, not verbatim historical transcript or model verdict",
        "visible_input": build_verifier_user_message(packet),
        "writer_hint": hints["configkit/config/writer.py"],
    })
    save("analysis/root-cause-tree.json", {
        "contract_extraction": {"status": "by_design", "evidence": ["A259 conservative zero-call fallback", "four production bootstrap tests", "historical task_contract_bootstrapped"], "limitation": "coarse coverage; no spec-body extraction"},
        "ledger_semantic_closure": {"status": "contract semantics underspecified outside tested rules", "evidence": ["mode matrix", "A257/A264", "required_evidence owns review requirement"], "bug_proven": False},
        "verification_authority": {"status": "by_design", "evidence": ["exact user command matches VerificationContract", "semantic certification explicitly says execution only"], "limitation": "broad behavior status overstates empirical semantic proof"},
        "verifier_context": {"status": "proven_gap", "evidence": ["before/focused-tests.txt", "ChangeTracker headings are not diff --git", "historical repeated diff and writer omission"], "fix": "single per-file section boundary"},
        "verifier_model_judgment": {"status": "unknown causal contribution", "evidence": ["full original spec present", "final accept despite unchanged defect", "no real calls in this audit"], "limitation": "corrected context does not guarantee revise"},
    })
    # Hash all baseline tracked production and historical evidence; compare using Git.
    tracked = subprocess.check_output(["git", "ls-tree", "-r", BASE, "nz_coder", "docs/evidence"], cwd=ROOT, text=True)
    changed = []
    source_hashes = {}
    for line in tracked.splitlines():
        metadata, relative = line.split("\t", 1)
        expected = metadata.split()[2]
        data = (ROOT / relative).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != expected:
            changed.append(relative)
        if relative.startswith("nz_coder/"):
            source_hashes[relative] = hashlib.sha256(data).hexdigest()
    assert changed == ["nz_coder/runtime/verification/sidecar_verifier.py"], changed
    save("analysis/source-hashes.json", source_hashes)
    for p in OUT.rglob("*"):
        if p.is_file() and p.suffix in {".json", ".txt", ".md", ".py"}:
            text = p.read_text()
            # Logs can contain local pytest stack paths. Publish placeholders.
            p.write_text(clean(text))
    forbidden_keys = {"reasoning_content", "authorization", "cookie", "api_key", "access_token"}

    def check_keys(value):
        if isinstance(value, dict):
            assert not (set(str(k).lower() for k in value) & forbidden_keys), "private payload field"
            for item in value.values():
                check_keys(item)
        elif isinstance(value, list):
            for item in value:
                check_keys(item)

    for p in OUT.rglob("*.json"):
        check_keys(read(p))
    # Match real credentials privately, never print values or entire environment.
    secrets = []
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
                value = value.strip().strip("\"'")
                if len(value) >= 12:
                    secrets.append(value)
    for p in OUT.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            text = p.read_text()
            assert not any(secret in text for secret in secrets), "secret scan failed"
            assert str(Path.home()) not in text, "personal path in evidence"
    save("integrity-audit.json", {
        "baseline": BASE, "historical_evidence_unchanged": True,
        "changed_production": changed, "paid_model_requests": 0,
        "real_samples": 0, "dependencies_installed": False,
        "network": "all test subprocesses under existing offline_exec seccomp; controlled planner has no client",
        "known_bad_workspace": "46 passed; 11/12; original hashes unchanged",
        "privacy": "JSON forbidden-field scan, exact credential match scan and personal-path scan passed",
    })
    save("SHA256SUMS.json", {str(p.relative_to(OUT)): digest(p) for p in sorted(OUT.rglob("*"))
                            if p.is_file() and p.name != "SHA256SUMS.json" and "__pycache__" not in p.parts})
    print("C offline audit: 46 project tests passed; 11/12 acceptance; history unchanged; 0 paid model requests")


if __name__ == "__main__":
    main()
