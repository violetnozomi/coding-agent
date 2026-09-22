"""Validate the measuring instruments offline; never run a coding Agent."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import socket

import pytest

SUITE = Path(__file__).parent / "fixtures/agent_core_diagnostic_v1"
spec = importlib.util.spec_from_file_location("diagnostic_validator", SUITE / "validate.py")
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)


@pytest.fixture(autouse=True)
def no_online_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No online Provider/network permitted in fixture tests")
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("task", suite.TASKS)
def test_initial_oracle_and_clean_rebuild(task):
    result = suite.validate_task(task)
    assert result["initial_project"]["exit"] == 0
    assert result["initial_acceptance"]["exit"] == 1
    assert result["oracle_project"]["exit"] == result["oracle_acceptance"]["exit"] == 0
    assert result["repeat_project"]["exit"] == result["repeat_acceptance"]["exit"] == 0
    assert result["deterministic_rebuild"]


@pytest.mark.parametrize("task", suite.TASKS)
def test_manifests_and_evaluator_boundary(task, tmp_path):
    manifest = json.loads((SUITE / task / "manifest.json").read_text())
    assert manifest["id"] == task
    assert manifest["network_required"] is False
    assert manifest["required_artifacts"] and manifest["diagnostic_target"]
    text = (SUITE / task / manifest["task_file"]).read_text().lower()
    assert not any(word in text for word in ("oracle", "acceptance", "diagnostic", "review_run_evidence"))
    workspace = tmp_path / "agent"
    initial = suite.prepare(task, workspace)
    assert initial == suite.prepare(task, tmp_path / "recreated")
    assert not any(part in {"oracle", "acceptance", "manifest.json", "task.md"}
                   for name in initial for part in Path(name).parts)
    assert not any(str(Path.home()) in p.read_text() for p in (SUITE / task).rglob("*") if p.is_file())
    assert not any("http://" in p.read_text() or "https://" in p.read_text()
                   for p in (SUITE / task / "acceptance").rglob("*.json"))
    assert all(name.startswith(("app/", "configkit/", "lib/", "tests/", "docs/"))
               or name in {"README.md", "CONFIG_SPEC.md", "index.cjs", "cli.cjs", "package.json"}
               for name in initial)


@pytest.mark.parametrize("task", suite.TASKS)
def test_snapshot_acceptance_is_non_mutating_and_repeatable(task, tmp_path):
    ws = tmp_path / "workspace"
    suite.prepare(task, ws)
    suite.apply_oracle(task, ws)
    before = suite.hashes(ws)
    assert suite.snapshot(ws, tmp_path / "version") == before
    first = suite.capture_version(task, ws, tmp_path / "capture")["acceptance"]
    assert json.loads((tmp_path / "capture/version.json").read_text())["mutation_generation"] is None
    second = suite.acceptance(task, ws)
    assert [(r["name"], r["passed"]) for r in first["checks"]] == [
        (r["name"], r["passed"]) for r in second["checks"]]
    assert suite.hashes(ws) == before


@pytest.mark.parametrize("family", ["AF_INET", "AF_INET6"])
def test_network_denied_in_child_and_grandchild(family, tmp_path):
    code = f"import socket; socket.socket(socket.{family})"
    direct = suite.execute(["python", "-c", code], tmp_path)
    child = suite.execute(["python", "-c",
                           f"import subprocess,sys; r=subprocess.run([sys.executable,'-c',{code!r}],timeout=3);sys.exit(r.returncode)"], tmp_path)
    assert direct["exit"] != 0 and child["exit"] != 0
    assert "PermissionError" in direct["stderr"] and "PermissionError" in child["stderr"]


def test_no_inherited_credentials_or_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel-not-a-credential")
    monkeypatch.setenv("SMOKE_SOCKET", "sentinel-provider-socket")
    result = suite.execute(["python", "-c", "import os; assert not any(k in os.environ for k in ['DEEPSEEK_API_KEY','SMOKE_SOCKET','HTTP_PROXY'])"], tmp_path)
    assert result["exit"] == 0


def test_timeout_is_failure_and_not_a_hang(tmp_path):
    result = suite.execute(["python", "-c", "while True: pass"], tmp_path, timeout=0.15)
    assert result["timed_out"] and result["exit"] != 0
    with pytest.raises(ValueError, match="unbounded"):
        suite.execute(["python", "-c", "pass"], tmp_path, timeout=0)


def test_unsettled_promise_cannot_silently_pass(tmp_path):
    task = "B_failure_recovery"
    ws = tmp_path / "workspace"
    suite.prepare(task, ws)
    (ws / "lib/scheduler.cjs").write_text("module.exports={schedule:()=>new Promise(()=>{})};\n")
    check = json.loads((SUITE / task / "acceptance/checks.json").read_text())[0]
    result = suite.execute(["node", "-e", check["code"]], ws)
    assert result["exit"] != 0
    assert "completion guard expired" in result["stderr"]


def test_c_authority_classification_and_original_snapshot(tmp_path):
    from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    task = "C_long_horizon"
    ws = tmp_path / "workspace"
    suite.prepare(task, ws)
    instruction = (SUITE / task / "task.md").read_text()
    path = ws / "CONFIG_SPEC.md"
    original = path.read_text()
    artifact = next(a for a in resolve_bootstrap_artifacts(instruction, workspace=ws).artifacts
                    if a.path == "CONFIG_SPEC.md")
    assert artifact.role == "task_reference" and artifact.authority == "task_spec"
    assert not artifact.required
    state = RuntimeState()
    state.bind_task_references(instruction, workspace=ws)
    ref = state.task_reference_evidence[0]
    assert ref["content_hash"] == hashlib.sha256(original.encode()).hexdigest()
    assert ref["complete"] and ref["text"] == original
    suite.apply_oracle(task, ws)
    assert path.read_text() == original
    path.write_text("Agent-owned later change")
    assert state.task_reference_evidence[0]["text"] == original


@pytest.mark.parametrize("task,path,old,new,failed_check", [
    ("A_unknown_location", "app/service.py", 'record["correlation_id"] = event.correlation_id',
     'record["correlation_id"] = "lost"', "public_api"),
    ("B_failure_recovery", "lib/queue.cjs", "this.pending.push(entry)",
     "this.pending.unshift(entry)", "fifo_fairness"),
    ("C_long_horizon", "configkit/commands/migrate.py", "dry_run=args.dry_run",
     "dry_run=False", "cli_migrate"),
])
def test_acceptance_detects_plausible_partial_implementation(task, path, old, new, failed_check, tmp_path):
    ws = tmp_path / "workspace"
    suite.prepare(task, ws)
    suite.apply_oracle(task, ws)
    target = ws / path
    assert old in target.read_text()
    target.write_text(target.read_text().replace(old, new))
    result = suite.acceptance(task, ws)
    assert result["exit"] == 1
    assert not next(row for row in result["checks"] if row["name"] == failed_check)["passed"]


def test_symlink_not_copied(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to(SUITE / "validate.py")
    with pytest.raises(ValueError, match="symlink"):
        suite.snapshot(source, tmp_path / "dest")


def test_all_harness_subprocesses_bounded_and_no_provider_import():
    tree = ast.parse((SUITE / "validate.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    waits = [node for node in calls if isinstance(node.func, ast.Attribute)
             and node.func.attr in {"communicate", "wait", "run"}]
    assert waits and all(any(k.arg == "timeout" for k in call.keywords) for call in waits)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any("providers" in name or "native_sdk" in name or "openai" in name for name in imports)


@pytest.mark.parametrize("task", suite.TASKS)
def test_every_check_maps_to_visible_requirement_and_no_fake_rows(task):
    mapping = json.loads((SUITE / task / "requirement-map.json").read_text())
    checks = json.loads((SUITE / task / "acceptance/checks.json").read_text())
    assert set(mapping) == {row["name"] for row in checks}
    for proof in mapping.values():
        assert proof["quote"] in (SUITE / task / proof["source"]).read_text()
    assert json.loads((SUITE / task / "causal-table.json").read_text()) == []
    divergence = json.loads((SUITE / task / "first_divergence.json").read_text())
    assert divergence["observed"] is False and divergence["classification"] is None


@pytest.mark.parametrize("task", suite.TASKS)
def test_missing_docs_remains_an_independent_failure(task, tmp_path):
    ws = tmp_path / "workspace"
    suite.prepare(task, ws)
    suite.apply_oracle(task, ws)
    doc = "docs/README.md" if task == "C_long_horizon" else "README.md"
    (ws / doc).write_bytes((SUITE / task / "workspace" / doc).read_bytes())
    result = suite.acceptance(task, ws)
    assert result["exit"] == 1
    assert [r["name"] for r in result["checks"] if not r["passed"]] == ["documentation"]


def test_capture_schema_includes_required_causal_fields():
    schema = json.loads((SUITE / "causal-table.schema.json").read_text())
    assert schema["type"] == "array"
    assert set(schema["items"]["required"]) == set(schema["items"]["properties"])
    assert {"purpose", "tool_calls", "provider_usage", "mutation_generation", "reference_state"} <= set(schema["items"]["required"])
    assert "permission_outcome" in schema["items"]["properties"]["tool_calls"]["items"]["required"]
