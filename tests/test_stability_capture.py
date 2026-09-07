"""The CI collector uploads only bounded structural, test-owned evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.stability_capture import collect_diagnostics, project_operation


def test_projection_rejects_secret_fields_and_dynamic_exception_names():
    raw = {
        "schema": "nz.diagnostic.v1", "id": "diag-" + "a" * 32,
        "component": "repo_map", "phase": "index_read", "version": "0.1.0",
        "message": "SENTINEL", "path": "SENTINEL", "nonce": "SENTINEL",
        "exceptions": [{"type": "SENTINEL", "message": "SENTINEL", "frames": [
            {"module": "SENTINEL", "function": "SENTINEL", "line": 12, "locals": "SENTINEL"},
        ]}],
        "facts": {"pid": 12, "nonce": "SENTINEL"},
        "identities": {"call_id": "SENTINEL"},
    }
    safe = project_operation(raw)
    assert "SENTINEL" not in json.dumps(safe)
    assert safe["exceptions"][0]["frames"][0]["line"] == 12
    assert safe["phase"] == "index_read"


def test_existing_module_does_not_authorize_arbitrary_frame_function():
    raw = {"schema": "nz.diagnostic.v1", "id": "diag-" + "a" * 32,
           "component": "repo_map", "phase": "index_read", "exceptions": [
               {"type": "OSError", "frames": [{"module": "nz_coder.state.diagnostics",
                                                "function": "SENTINEL_secret_function", "line": 12}]}]}
    safe = project_operation(raw)
    assert "SENTINEL" not in json.dumps(safe)
    assert safe["exceptions"][0]["frames"][0]["line"] == 12


def test_collector_reads_only_diagnostic_envelopes_inside_owned_root(tmp_path):
    from nz_coder.state.diagnostics import OperationDiagnostic

    owned = tmp_path / "owned"
    diag = OperationDiagnostic("daemon", directory=owned / "diagnostics" / "parent")
    diag.advance("spawn")
    diag.failure(OSError("SENTINEL"))
    (owned / "raw.log").write_text("SENTINEL")
    result = collect_diagnostics([owned])
    assert any(row["id"] == diag.id for row in result["records"])
    assert result["status"] == "collected"
    assert "SENTINEL" not in json.dumps(result)


def test_collector_bounds_file_content_and_reports_missing(tmp_path):
    assert collect_diagnostics([tmp_path / "absent"])["status"] == "missing"
    root = tmp_path / "diagnostics"
    root.mkdir()
    (root / "diagnostic__diag-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jsonl").write_text("x" * 90000)
    result = collect_diagnostics([tmp_path], max_bytes=100)
    assert result["truncated"]
    assert result["bytes_read"] <= 101
    assert "x" * 20 not in json.dumps(result)


def test_collector_does_not_follow_external_links(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    target = tmp_path / "SENTINEL"
    target.mkdir()
    try:
        (root / "diagnostics").symlink_to(target, target_is_directory=True)
    except OSError:
        # Exercise native reparse classification even without symlink privilege.
        import tests.stability_capture as capture

        (root / "diagnostics").mkdir()
        original = capture._alias
        marker = (root / "diagnostics").stat().st_ino
        monkeypatch.setattr(capture, "_alias", lambda info: info.st_ino == marker or original(info))
    result = collect_diagnostics([root])
    assert not result["records"]
    assert "SENTINEL" not in json.dumps(result)


def test_collection_exception_preserves_pytest_failure_and_reports_gap(tmp_path, monkeypatch):
    import tests.stability_capture as capture

    def broken(*args, **kwargs):
        raise OSError("SENTINEL-private-collector-error")

    monkeypatch.setattr(capture, "collect_diagnostics", broken)
    plugin = capture._Capture(tmp_path / "evidence")
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    item = SimpleNamespace(nodeid="tests/test_daemon.py::test_failure",
                           funcargs={"tmp_path": tmp_path},
                           config=SimpleNamespace(_tmp_path_factory=factory))
    report = SimpleNamespace(when="call", failed=True, passed=False, outcome="failed")
    hook = plugin.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.outcome == "failed"
    assert plugin.collection_failed
    assert len(plugin.tests) == 1
    assert plugin.tests[0]["outcome"] == "failed"
    assert plugin.tests[0]["diagnostics"]["status"] == "collection_failed"
    plugin.flush()
    assert "SENTINEL" not in (tmp_path / "evidence" / "tests.json").read_text()


def test_ci_preserves_exit_and_uploads_only_attempt_scoped_safe_artifacts():
    root = Path(__file__).resolve().parents[1]
    for name in ("core-runtime.yml", "windows-product-rc.yml"):
        workflow = (root / ".github" / "workflows" / name).read_text()
        assert "tests.stability_capture" in workflow
        assert "path: artifacts/safe/" in workflow
        assert "github.run_attempt" in workflow
        assert "--self-test" in workflow
        assert "continue-on-error" not in workflow
        assert "Tee-Object" not in workflow
        assert "| tee" not in workflow


def test_postprocess_metadata_failure_cannot_erase_known_pytest_exit(tmp_path, monkeypatch):
    import tests.stability_capture as capture

    monkeypatch.setattr(capture.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=4))

    def broken():
        raise OSError("SENTINEL-metadata-error")

    monkeypatch.setattr(capture, "_versions", broken)
    output = tmp_path / "evidence"
    assert capture.main(["--output", str(output), "--", "tests/test_daemon.py"]) == 4
    assert "SENTINEL" not in (output / "run.json").read_text()


def test_budget_applies_to_actual_serialized_artifact(tmp_path, monkeypatch):
    import tests.stability_capture as capture

    monkeypatch.setattr(capture, "_MAX_ARTIFACT_BYTES", 8192)
    sample = {"status": "collected", "records": [
        {"schema": "nz.diagnostic.v1", "id": "diag-" + "a" * 32, "component": "daemon",
         "exceptions": [{"type": "OSError", "frames": [
             {"module": "external", "function": "external", "line": 1} for _ in range(8)
         ]} for _ in range(6)]}
    ]}
    monkeypatch.setattr(capture, "collect_diagnostics", lambda *args, **kwargs: sample)
    plugin = capture._Capture(tmp_path / "evidence")
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    item = SimpleNamespace(nodeid="tests/test_daemon.py::test_budget", funcargs={"tmp_path": tmp_path},
                           config=SimpleNamespace(_tmp_path_factory=factory))
    report = SimpleNamespace(when="call", failed=False, passed=True, outcome="passed")
    for _ in range(5):
        hook = plugin.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda: report))
    plugin.flush()
    assert (tmp_path / "evidence" / "tests.json").stat().st_size <= 8192


def test_controlled_failed_subprocess_retains_safe_evidence_after_tmp_cleanup(tmp_path):
    output = tmp_path / "evidence"
    command = [sys.executable, "-m", "tests.stability_capture", "--self-test", "--output", str(output)]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads((output / "tests.json").read_text())
    run = json.loads((output / "run.json").read_text())
    assert run["exit_code"] == 1  # Self-test checks this, never rewrites the real exit.
    assert run["controlled_failure"] is True
    assert any(row["outcome"] == "failed" for row in records["tests"])
    assert any(row["diagnostics"]["records"] for row in records["tests"])
    assert not run["private_output_retained"]
    serialized = "".join(path.read_text() for path in output.glob("*.json"))
    assert "SENTINEL" not in serialized + result.stdout + result.stderr


def test_collection_failure_retains_safe_cause_and_real_exit(tmp_path):
    specimen = tmp_path / "test_SENTINEL_private_filename.py"
    specimen.write_text('raise ImportError("SENTINEL-private-import")\n', encoding="utf-8")
    output = tmp_path / "evidence"
    result = subprocess.run(
        [sys.executable, "-m", "tests.stability_capture", "--output", str(output), "--",
         "--confcutdir", str(tmp_path), str(specimen)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    records = json.loads((output / "tests.json").read_text())
    run = json.loads((output / "run.json").read_text())
    assert run["exit_code"] == 2
    assert not run["private_output_retained"]
    failures = [row for row in records["tests"] if row["phase"] == "collect"]
    assert len(failures) == 1
    assert failures[0]["node_id"] == "external_test"
    assert any(error["type"] == "ImportError" for error in failures[0]["exceptions"])
    assert "SENTINEL" not in json.dumps(records) + json.dumps(run) + result.stdout + result.stderr


def test_controlled_specimen_does_not_enumerate_unowned_ancestors(tmp_path, monkeypatch):
    import tests.stability_capture as capture

    guard = tmp_path / "collection_guard.py"
    guard.write_text(
        'import os\nfrom pathlib import Path\n'
        'def pytest_sessionstart(session):\n'
        '    ancestors = set(Path(session.config.args[0]).resolve().parent.parents)\n'
        '    original = os.scandir\n'
        '    def restricted(path):\n'
        '        if isinstance(path, (str, os.PathLike)) and Path(path).resolve() in ancestors:\n'
        '            raise PermissionError("SENTINEL-unowned-ancestor")\n'
        '        return original(path)\n'
        '    os.scandir = restricted\n', encoding="utf-8",
    )
    original = subprocess.run

    def with_guard(command, **kwargs):
        # Keep real pytest collection/subprocess behavior; only deny directory
        # enumeration above its owned specimen, like restricted Windows parents.
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(tmp_path), str(capture._REPO))))
        return original([*command, "-p", "collection_guard"], env=env, **kwargs)

    monkeypatch.setattr(capture.subprocess, "run", with_guard)
    output = tmp_path / "evidence"
    assert capture.main(["--self-test", "--output", str(output)]) == 0
    run = json.loads((output / "run.json").read_text())
    assert run["exit_code"] == 1
    assert not run["private_output_retained"]
    assert "SENTINEL" not in "".join(path.read_text() for path in output.glob("*.json"))


def test_external_source_alias_cannot_authorize_its_private_name(tmp_path, monkeypatch):
    import tests.stability_capture as capture

    alias = tmp_path / "test_SENTINEL_private_filename.py"
    target = Path(__file__).resolve()
    try:
        alias.symlink_to(target)
    except OSError:
        # Native Windows may not grant symlink creation. The lexical boundary
        # must also hold if resolution reports an existing trusted target.
        original = Path.resolve
        monkeypatch.setattr(Path, "resolve", lambda self, *args, **kwargs:
                            target if self == alias else original(self, *args, **kwargs))
    result = capture._node(str(alias) + "::test_SENTINEL_private_function")
    assert result["node_id"] == "external_test"
    assert "SENTINEL" not in json.dumps(result)
    assert capture._node("tests/test_stability_capture.py")["node_id"] == "test_stability_capture.py"
