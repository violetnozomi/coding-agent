"""Opt-in pytest/CI evidence capture, without exporting raw test output.

The runner retains the actual pytest exit. Only explicit test-owned trees are
inspected, before fixture teardown; private output is temporary and never an
artifact. This is test infrastructure, not a production diagnostic authority.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile

import pytest

from nz_coder import __version__
from nz_coder.state.diagnostics import (
    SCHEMA, _FACTS, _PHASES, exception_evidence, safe_exception_label, safe_frame,
)


_REPO = Path(__file__).resolve().parents[1]
_HEX = re.compile(r"^[a-f0-9]{64}$")
_DIAG = re.compile(r"^diag-[a-f0-9]{32}$")
_FILE = re.compile(r"^diagnostic__diag-[a-f0-9]{32}\.jsonl$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_RECORDS = 512


def _exception_type(value):
    return safe_exception_label(value) if isinstance(value, str) else "Exception"


def project_operation(value: object) -> dict | None:
    """Reconstruct the allowlisted schema; never copy a dictionary wholesale."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    identifier = value.get("id")
    if not isinstance(identifier, str) or not _DIAG.fullmatch(identifier):
        return None
    component = value.get("component")
    if component not in {"daemon", "repo_map", "workflow"}:
        return None
    result = {"schema": SCHEMA, "id": identifier, "component": component, "version": __version__}
    for key in ("phase", "primary_phase", "last_completed_phase"):
        result[key] = value.get(key) if value.get(key) in _PHASES else "unknown"
    result["primary_type"] = _exception_type(value.get("primary_type"))
    identities = value.get("identities")
    result["identities"] = {
        key: item for key, item in identities.items()
        if key in {"session_id", "interaction_id", "call_id", "run_id", "workspace_id"}
        and isinstance(item, str) and _HEX.fullmatch(item)
    } if isinstance(identities, dict) else {}
    facts = value.get("facts")
    result["facts"] = {
        key: item for key, item in facts.items()
        if key in _FACTS and (type(item) in (bool, int) or item is None)
    } if isinstance(facts, dict) else {}
    chain = value.get("exceptions")
    result["exceptions"] = []
    for entry in (chain[:6] if isinstance(chain, list) else []):
        if not isinstance(entry, dict):
            continue
        frames = entry.get("frames")
        safe_frames = []
        for frame in (frames[:24] if isinstance(frames, list) else []):
            if not isinstance(frame, dict):
                continue
            module, function = "external", "external"
            parts = str(frame.get("module", "")).split(".")
            name = frame.get("function")
            if (parts[0] == "nz_coder" and all(_NAME.fullmatch(part) for part in parts)
                    and _REPO.joinpath(*parts).with_suffix(".py").is_file()
                    and isinstance(name, str) and _NAME.fullmatch(name)):
                module, function = ".".join(parts), name
            line = frame.get("line")
            safe_frames.append(safe_frame(module, function, line))
        kind = safe_exception_label(str(entry.get("type", "")), str(entry.get("type_module", "")))
        safe_entry = {"type": kind, "frames": safe_frames}
        for key in ("frames_omitted", "frames_truncated", "chain_truncated", "capture_failed"):
            if type(entry.get(key)) in (int, bool):
                safe_entry[key] = entry[key]
        result["exceptions"].append(safe_entry)
    return result


def _alias(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def collect_diagnostics(roots: list[Path], *, max_bytes: int = 256 * 1024) -> dict:
    """Bound traversal and bytes, reject aliases and all non-diagnostic filenames."""
    result = {"status": "missing", "records": [], "bytes_read": 0,
              "truncated": False, "collection_failed": False, "files": 0}
    pending = [(Path(root), 0) for root in roots]
    visited = 0
    while pending and visited < 1024 and len(result["records"]) < _MAX_RECORDS:
        path, depth = pending.pop()
        visited += 1
        try:
            info = path.lstat()
            if _alias(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                if depth < 10:
                    with os.scandir(path) as entries:
                        for entry in entries:
                            if len(pending) + visited >= 1024:
                                result["truncated"] = True
                                break
                            pending.append((Path(entry.path), depth + 1))
                else:
                    result["truncated"] = True
                continue
            if not stat.S_ISREG(info.st_mode) or not _FILE.fullmatch(path.name):
                continue
            if "diagnostics" not in path.parts:
                continue
            remaining = max(0, min(64 * 1024, max_bytes - result["bytes_read"]))
            if not remaining:
                result["truncated"] = True
                break
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    result["collection_failed"] = True
                    continue
                data = handle.read(remaining + 1)
            result["files"] += 1
            result["bytes_read"] += len(data)
            truncated = len(data) > remaining
            result["truncated"] |= truncated
            lines = data[:remaining].splitlines()
            if truncated:
                lines = lines[:-1]  # Do not parse a cut JSON row.
            for line in lines[:64]:
                if len(result["records"]) >= _MAX_RECORDS:
                    result["truncated"] = True
                    break
                try:
                    row = json.loads(line)
                    record = project_operation(row.get("operation") if isinstance(row, dict) else None)
                    if record is not None:
                        result["records"].append(record)
                except (ValueError, TypeError, UnicodeError):
                    result["collection_failed"] = True
        except FileNotFoundError:
            continue
        except Exception:
            result["collection_failed"] = True
    result["truncated"] |= bool(pending)
    if result["collection_failed"]:
        result["status"] = "collection_failed"
    elif result["records"]:
        result["status"] = "collected"
    return result


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))


def _node(value: str) -> dict:
    static = value.split("[", 1)[0].replace("\\", "/")
    parts = static.split("::")
    filename = parts[0].rsplit("/", 1)[-1]
    valid = re.fullmatch(r"test_[A-Za-z0-9_]+\.py", filename)
    label = filename if valid else "external_test"
    label += "".join("::" + part for part in parts[1:] if _NAME.fullmatch(part))
    return {"node_id": label, "node_ref": hashlib.sha256(value.encode()).hexdigest()}


class _Capture:
    def __init__(self, output: Path):
        self.output = output
        self.tests: list[dict] = []
        self.remaining = max(0, _MAX_ARTIFACT_BYTES - 1024)  # Envelope/omission fields.
        self.omitted = 0
        self.collection_failed = False

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when == "setup" and report.passed:
            return
        if report.when == "teardown" and not report.failed:
            return
        try:
            evidence = {"status": "not_requested", "records": []}
            temporary = item.funcargs.get("tmp_path")
            selected = any(part in item.nodeid for part in (
                "test_daemon", "test_repo_languages", "test_repo_map_diagnostics",
                "test_workflow", "test_stability", "test_controlled_failure",
            ))
            if isinstance(temporary, Path) and (selected or report.failed):
                base = item.config._tmp_path_factory.getbasetemp().resolve()
                temporary.resolve().relative_to(base)
                roots = [temporary] + [temporary.parent / f"{prefix}-{temporary.name}"
                                       for prefix in ("user-state", "local-app-data")]
                evidence = collect_diagnostics(roots, max_bytes=min(256 * 1024, self.remaining))
            row = {**_node(item.nodeid), "phase": report.when, "outcome": report.outcome,
                   "diagnostics": evidence}
            if call.excinfo is not None:
                row["exception_type"] = _exception_type(call.excinfo.type.__name__)
                # Same structural cause projection as production: no messages,
                # locals, source lines, or arbitrary traceback paths.
                row["exceptions"] = exception_evidence(call.excinfo.value)
            self._admit(row, flush=report.failed)
        except Exception:
            self.collection_failed = True
            # Collector failures must not replace pytest's original report.

    def _admit(self, row: dict, *, flush: bool) -> None:
        size = len(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()) + 1
        if size <= self.remaining:
            self.tests.append(row)
            self.remaining -= size
        else:
            self.omitted += 1
        if flush:
            self.flush()  # Persist failure before temporary fixture cleanup.

    def pytest_exception_interact(self, node, call, report):
        # Collection errors never reach runtest_makereport. Capture the actual
        # exception here, without rendering CollectReport.longrepr/source text.
        if report.when != "collect" or call.excinfo is None:
            return
        try:
            self._admit({
                **_node(node.nodeid), "phase": "collect", "outcome": "failed",
                "diagnostics": {"status": "not_requested", "records": []},
                "exception_type": _exception_type(call.excinfo.type.__name__),
                "exceptions": exception_evidence(call.excinfo.value),
            }, flush=True)
        except Exception:
            self.collection_failed = True

    def flush(self):
        _write_json(self.output / "tests.json", {
            "schema": "nz.test_evidence.v1", "tests": self.tests,
            "records_omitted": self.omitted, "collection_failed": self.collection_failed,
        })

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            self.flush()
        except Exception:
            print("Evidence collection unavailable.", file=sys.stderr)


def pytest_addoption(parser):
    parser.addoption("--stability-evidence-dir", default="")


def pytest_configure(config):
    output = config.getoption("--stability-evidence-dir")
    if output:
        config.pluginmanager.register(_Capture(Path(output)), "scoped-stability-evidence")


def _versions() -> dict:
    result = {}
    for name in ("nz-coder", "pytest", "openai", "tree-sitter", "watchfiles"):
        try:
            value = metadata.version(name)
            result[name] = value if re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,60}", value) else "unknown"
        except metadata.PackageNotFoundError:
            result[name] = "not_installed"
        except Exception:
            result[name] = "unavailable"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    options, arguments = parser.parse_known_args(argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    output = options.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("evidence output must be empty; preserve previous attempts")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nz-ci-evidence-", ignore_cleanup_errors=True) as temporary:
        private = Path(temporary)
        if options.self_test:
            test = private / "test_controlled_failure.py"
            # Controlled specimen: its own fixture removes the source evidence
            # after makereport; raw failure output must never reach artifacts.
            test.write_text(
                'import shutil\nimport pytest\n'
                'from nz_coder.state.diagnostics import OperationDiagnostic\n'
                '@pytest.fixture\ndef owned(tmp_path):\n'
                '    yield tmp_path\n    shutil.rmtree(tmp_path)\n'
                'def test_controlled_failure(owned, tmp_path):\n'
                '    diag = OperationDiagnostic("daemon", directory=owned / "diagnostics")\n'
                '    diag.advance("service_init")\n'
                '    diag.failure(ValueError("SENTINEL-secret-nonce-input"))\n'
                '    assert False, "SENTINEL-private-exception"\n', encoding="utf-8",
            )
            arguments = [str(test)]
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p",
                   "tests.stability_capture", "--stability-evidence-dir", str(output), *arguments]
        with (private / "pytest-private.log").open("wb") as stream:
            completed = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
        try:
            private_bytes = (private / "pytest-private.log").stat().st_size
        except OSError:
            private_bytes = None
    try:
        versions = _versions()
    except Exception:
        versions = {"collection_status": "unavailable"}
    run = {
        "schema": "nz.test_run.v1", "exit_code": completed.returncode,
        "controlled_failure": options.self_test, "private_output_retained": private.exists(),
        "private_output_bytes": private_bytes,
        "platform": sys.platform, "python": platform.python_version(), "dependencies": versions,
        "command_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
        "command": ["python", "-m", "pytest", "-q", "--tb=short", "-p", "tests.stability_capture",
                    "--stability-evidence-dir", "[job evidence output]", *[
            value if re.fullmatch(r"tests/[A-Za-z0-9_./]+(?:\.py)?", value)
            or value in {"-q", "--tb=short"} else "[selection-redacted]" for value in arguments
        ]],
        "evidence_present": (output / "tests.json").is_file(),
    }
    for key in ("GITHUB_SHA", "GITHUB_WORKFLOW", "GITHUB_JOB", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        value = os.environ.get(key, "")
        run[key.lower()] = value if re.fullmatch(r"[A-Za-z0-9_ .-]{0,120}", value) else "redacted"
    try:
        _write_json(output / "run.json", run)
    except Exception:
        print("Run evidence unavailable; pytest exit is unchanged.", file=sys.stderr)
        return completed.returncode if not options.self_test else 1
    print(f"pytest exit={completed.returncode}; safe evidence captured={run['evidence_present']}")
    if run["evidence_present"]:
        try:
            captured = json.loads((output / "tests.json").read_text())
            print("; ".join(f"{name}={sum(row['outcome'] == name for row in captured['tests'])}"
                            for name in ("passed", "failed", "skipped")))
        except Exception:
            print("Test totals unavailable.")
    if options.self_test:
        text = "".join(path.read_text() for path in output.glob("*.json"))
        records = json.loads((output / "tests.json").read_text()) if run["evidence_present"] else {}
        valid = (completed.returncode == 1 and "SENTINEL" not in text
                 and any(item["outcome"] == "failed" and item["diagnostics"]["records"]
                         for item in records.get("tests", [])))
        return 0 if valid else 1
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
