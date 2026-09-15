"""Opt-in real SA CLI / production Runner replay; no external model or fake tools."""
from __future__ import annotations

import difflib
import ctypes.util
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
from socketserver import UnixStreamServer
import subprocess
import sys
import tempfile
import threading

import pytest

from nz_coder.evaluation.behavioral import _fixture_f
from nz_coder.evaluation.reference_adapter import (
    InfCodeXReferenceAdapter, ReferenceBehaviorDriver, ReferenceRunRequest,
    ReferenceCapability, _execute, _workspace_hashes,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(sys.platform != "linux" or not ctypes.util.find_library("seccomp"),
                    reason="Linux libseccomp is required for inherited network denial")
def test_offline_launcher_blocks_network_after_exec_and_child_spawn():
    for family in ("AF_INET", "AF_INET6"):
        child = f"import socket; socket.socket(socket.{family})"
        grandchild = f"import subprocess,sys; raise SystemExit(subprocess.call([sys.executable,'-c',{child!r}]))"
        result = subprocess.run([sys.executable, str(FIXTURES / "offline_exec.py"),
                                 sys.executable, "-c", grandchild], capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "PermissionError" in result.stderr
        assert "sandbox: IPv4/IPv6 sockets denied" in result.stderr


class ControlledProtocol:
    """Return protocol messages based solely on actual received tool evidence."""

    def __init__(self, side, output):
        self.side, self.output, self.calls = side, output, 0

    def respond(self, request):
        self.calls += 1
        messages = request["messages"]
        results = [m for m in messages if m.get("role") == "tool"]
        latest = str(results[-1].get("content")) if results else ""
        schemas = {t["function"]["name"] for t in request.get("tools", [])}
        read, edit = ("read", "edit") if self.side == "infcodex" else ("read_file", "edit_file")
        step = self.calls
        if step == 1:
            assert not results
            name, args = read, {"path": "calc/service.py"}
        elif step == 2:
            assert "return total / count" in latest, latest
            name, args = "bash", {"command": "python -m pytest -q"}
        elif step == 3:
            assert "ZeroDivisionError" in latest and "test_empty_count" in latest, latest
            name = edit
            old, new = "return total / count", "return 0 if count == 0 else total / count"
            args = {"path": "calc/service.py"}
            args.update({"old_string": old, "new_string": new} if self.side == "infcodex"
                        else {"old_text": old, "new_text": new})
        elif step == 4:
            assert results and "Error" not in latest and "error" not in latest, latest
            assert "calc/service.py" in latest or "Successfully" in latest, latest
            name, args = "bash", {"command": "python -m pytest -q"}
        else:
            assert step == 5, "Unexpected extra/auxiliary request; do not silently simulate it"
            assert "1 passed" in latest, latest
            return {"role": "assistant", "content": "Fixed ratio for an empty count. python -m pytest -q: 1 passed."}
        assert name in schemas, (name, schemas)
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"smoke-{step}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}


def _handler(protocol):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with (protocol.output / "requests.jsonl").open("a") as stream:
                stream.write(json.dumps({"path": self.path, "request": payload}) + "\n")
            try:
                assert self.path == "/v1/chat/completions"
                message = protocol.respond(payload)
                usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
                finish = "tool_calls" if message.get("tool_calls") else "stop"
                body = {"id": f"local-{protocol.calls}", "object": "chat.completion",
                        "created": 1, "model": "local-smoke", "usage": usage,
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
                with (protocol.output / "responses.jsonl").open("a") as stream:
                    stream.write(json.dumps(body) + "\n")
                if payload.get("stream"):
                    delta = dict(message)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [{**t, "index": i} for i, t in enumerate(delta["tool_calls"])]
                    chunks = [{**body, "object": "chat.completion.chunk", "usage": None,
                               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                              {**body, "object": "chat.completion.chunk",
                               "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
                    data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    data, content_type = json.dumps(body).encode(), "application/json"
                self.send_response(200)
            except Exception as exc:
                with (protocol.output / "protocol-errors.txt").open("a") as stream:
                    stream.write(repr(exc) + "\n")
                data = json.dumps({"error": {"message": str(exc), "type": "offline_contract_error"}}).encode()
                content_type = "application/json"
                self.send_response(400)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Handler


def _environment(home, output, socket_path, prompt):
    # No API keys, proxies, private config, MCP or previous session variables.
    return {"HOME": str(home), "KODAX_HOME": str(home / ".kodax"),
            "PATH": f"{output.parent / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "SMOKE_API_KEY": "local-test-only",
            "SMOKE_SOCKET": str(socket_path), "SMOKE_OUTPUT": str(output),
            "SMOKE_PROMPT": prompt, "TERM": "dumb", "NO_COLOR": "1"}


@pytest.mark.skipif(os.environ.get("NZ_RUN_PAIRED_SMOKE") != "1",
                    reason="opt-in: requires locally built pinned InfCodeX, Node LTS and Linux libseccomp")
def test_real_paired_entrypoints(tmp_path, monkeypatch):
    reference = ROOT / "references/InfCodeX"
    node = Path(os.environ["NZ_REFERENCE_NODE"]).resolve()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=reference, text=True).strip() == "d3a812379b589597347f5be12d5b68477e577f02"
    output = Path(os.environ.get("NZ_SMOKE_OUTPUT_DIR", str(tmp_path / "evidence"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "bin").mkdir(exist_ok=True)
    for name, target in (("node", node), ("python", Path(sys.executable))):
        link = output / "bin" / name
        if not link.exists():
            link.symlink_to(target)
    monkeypatch.setenv("PATH", str(output / "bin") + os.pathsep + os.environ["PATH"])
    capability = InfCodeXReferenceAdapter(reference).probe()
    assert capability.available, capability.reason
    initial = []
    summaries = {}
    for side in ("infcodex", "nzcoder"):
        side_output = output / side
        side_output.mkdir()  # never overwrite a previous trial
        workspace = side_output / "workspace"
        workspace.mkdir()
        task = _fixture_f(workspace)
        before = _workspace_hashes(workspace)
        initial.append(before)
        original = (workspace / "calc/service.py").read_text()
        home = side_output / "home"
        (home / ".kodax").mkdir(parents=True)
        (home / ".kodax/config.json").write_text(json.dumps({"customProviders": [{
            "name": "local-smoke", "protocol": "openai", "baseUrl": "http://localhost/v1",
            "apiKeyEnv": "SMOKE_API_KEY", "model": "local-smoke", "reasoning": "none",
        }]}))
        protocol = ControlledProtocol(side, side_output)
        with tempfile.TemporaryDirectory(prefix="nzuds-") as sockets:
            socket_path = Path(sockets) / "provider.sock"
            server = UnixStreamServer(str(socket_path), _handler(protocol))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = _environment(home, side_output, socket_path, task.prompt)
            launcher = [sys.executable, str(FIXTURES / "offline_exec.py")]
            try:
                if side == "infcodex":
                    preparation = launcher + [str(node), "--import",
                        str(reference / "node_modules/tsx/dist/esm/index.mjs"),
                        str(FIXTURES / "prepare-reference-session.mjs"), str(reference / "src/sdk-runtime.ts")]
                    prepared = subprocess.run(preparation, cwd=workspace, env=env,
                                              capture_output=True, text=True, timeout=30)
                    (side_output / "session-preparation.json").write_text(json.dumps({
                        "argv": preparation, "exit_code": prepared.returncode,
                        "stdout": prepared.stdout, "stderr": prepared.stderr}, indent=2))
                    assert prepared.returncode == 0, prepared.stderr
                    command = launcher + [str(node), "--import", str(FIXTURES / "unix-fetch.mjs"),
                        *capability.command[1:], "--mode", "json", task.prompt,
                        "--provider", "local-smoke", "--model", "local-smoke",
                        "--agent-mode", "sa", "--reasoning", "off", "--max-iter", "8", "--session", "offline-F"]
                    result = _execute("InfCodeX", capability, command,
                        ReferenceRunRequest(workspace, task.prompt, "local-smoke", "local-smoke", timeout_s=90),
                        environment=env)
                    (side_output / "stdout.jsonl").write_text(result.raw_stdout)
                    (side_output / "stderr.txt").write_text(result.raw_stderr)
                    (side_output / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
                    normalized = ReferenceBehaviorDriver._normalize(result.trajectory)
                    (side_output / "normalized.json").write_text(json.dumps(normalized, indent=2))
                    status = result.status
                else:
                    command = launcher + [sys.executable, str(FIXTURES / "native-smoke.py")]
                    result = _execute("NZ-Coder", ReferenceCapability("NZ-Coder", True, None), command,
                        ReferenceRunRequest(workspace, task.prompt, "local-smoke", "openai-compatible", timeout_s=90),
                        environment=env)
                    (side_output / "stdout.txt").write_text(result.raw_stdout)
                    (side_output / "stderr.txt").write_text(result.raw_stderr)
                    status = result.status
                (side_output / "command.json").write_text(json.dumps({"argv": command, "env": env}, indent=2))
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
        assert status == "completed", (side, side_output, status)
        assert protocol.calls == 5, (side, protocol.calls)
        assert not (side_output / "protocol-errors.txt").exists()
        if side == "infcodex":
            tools = [e for e in normalized if e["event"] == "tool_call"]
            assert len(tools) == 4
            assert all(e["status"] == "unknown" for e in tools)
            assert "ZeroDivisionError" in tools[1]["output"] and "1 passed" in tools[3]["output"]
            assert result.tokens is None
        else:
            effective = json.loads((side_output / "effective.json").read_text())
            assert effective["repo_retrieval_strategy"] == "policy"
            assert effective["repo_retrieval_strategy_override"] is None
            trace_files = list(home.rglob("offline-F__*.jsonl"))
            assert len(trace_files) == 1
            shutil.copyfile(trace_files[0], side_output / "runtime.jsonl")
            raw = [json.loads(line) for line in trace_files[0].read_text().splitlines()]
            normalized = [e for e in raw if e["event"] in {
                "tool_call", "llm_response", "verification_result", "run_end"}]
            (side_output / "normalized.json").write_text(json.dumps(normalized, indent=2))
            tools = [e for e in raw if e["event"] == "tool_call"]
            assert [e["name"] for e in tools] == ["read_file", "bash", "edit_file", "bash"]
            assert [e["status"] for e in tools] == ["ok", "nonzero", "ok", "ok"]
            assert all(e["executed"] and not e["dispatch_failed"] for e in tools)
            assert tools[1]["command_failed"] is True
            assert [e["status"] for e in raw if e["event"] == "verification_result"] == ["failed", "passed"]
            terminal = next(e for e in raw if e["event"] == "run_end")
            assert terminal["status"] == "completed"
            assert terminal["runtime"]["provider_calls_by_purpose"] == {"coding": 5}
            assert terminal["runtime"]["mutation_generation"] == 1
        after = _workspace_hashes(workspace)
        assert before["tests/test_ratio.py"] == after["tests/test_ratio.py"]
        diff = "".join(difflib.unified_diff(original.splitlines(True),
                    (workspace / "calc/service.py").read_text().splitlines(True),
                    fromfile="a/calc/service.py", tofile="b/calc/service.py"))
        (side_output / "change.diff").write_text(diff)
        frozen = side_output / "frozen"
        shutil.copytree(workspace / "calc", frozen / "calc")
        shutil.copytree(workspace / "tests", frozen / "tests")
        retest_command = launcher + [sys.executable, "-m", "pytest", "-q"]
        acceptance = subprocess.run(retest_command,
                                    cwd=frozen, env=env, capture_output=True, text=True, timeout=30)
        # Hidden functional acceptance appears only after the Agent has terminated.
        hidden_command = launcher + [sys.executable, "-c",
            "from calc.service import ratio; assert ratio(10,0)==0; assert ratio(12,3)==4; assert ratio(-6,2)==-3"]
        hidden = subprocess.run(hidden_command,
            cwd=frozen, env=env, capture_output=True, text=True, timeout=30)
        evidence = {"initial_hashes": before, "final_hashes": after, "status": status,
                    "actual_provider_requests": protocol.calls, "auxiliary_requests": 0,
                    "usage_kind": "synthetic test values, not efficiency evidence",
                    "wall_time_ms": result.wall_time_ms,
                    "independent_retest": {"argv": retest_command, "exit_code": acceptance.returncode, "stdout": acceptance.stdout, "stderr": acceptance.stderr},
                    "hidden_acceptance": {"argv": hidden_command, "exit_code": hidden.returncode, "stdout": hidden.stdout, "stderr": hidden.stderr}}
        (side_output / "acceptance.json").write_text(json.dumps(evidence, indent=2))
        assert acceptance.returncode == hidden.returncode == 0, evidence
        summaries[side] = evidence
    assert initial[0] == initial[1]
    (output / "summary.json").write_text(json.dumps(summaries, indent=2))
