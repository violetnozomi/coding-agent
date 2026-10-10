"""原生测试入口的真实 CLI/容器回归；只替换收费模型边界。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(os.environ.get("NZ_SWE_SYMPY_SMOKE") != "1",
                   reason="requires prepared local SymPy image and public base; never downloads dependencies")
@pytest.mark.parametrize("mode", ["health", "failure", "empty"], ids=["sympy-health", "nonzero-test", "zero-tests"])
def test_native_bin_test_through_isolated_swe_cli(tmp_path, mode):
    from nz_coder.evaluation.model_relay import InputAccounting, RelayBinding, RelayLedger, RelayLimits, RelayServer
    from nz_coder.swebench.orchestrator import _collect_diff

    root = Path(__file__).resolve().parents[2]
    image = "nz-swe-sympy-bound:20261011"
    inspect = subprocess.run(["docker", "image", "inspect", image], check=True, capture_output=True, text=True)
    spec = importlib.util.spec_from_file_location("existing_review_launcher", root / "scripts/review_effects_execution.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    code = tmp_path / "code-new" / "nz_coder"
    shutil.copytree(root / "nz_coder", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.orig", "*.rej"))
    for child in (code / "evaluation").iterdir():
        if child.name not in {"__init__.py", "reproducibility.py"}:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    case = tmp_path / ("native-runner-" + uuid.uuid4().hex[:8])
    for name in ("workspace", "home", "tmp", "input", "sockets", "result", "control", "driver"):
        (case / name).mkdir(parents=True)
    packet = json.loads((root / "docs/evidence/swebench-official-protocol-2026-10-10/inputs/calibration.json").read_text())
    packet["instances"] = packet["instances"][:1]
    instance = packet["instances"][0]
    negative = mode != "health"
    failed = mode == "failure"
    if negative:
        source = tmp_path / "negative-base"
        (source / "bin").mkdir(parents=True)
        (source / "bin/test").write_text(
            "import sys\nprint('1 passed is only text, not a successful exit')\n"
            "print('AssertionError: controlled negative test')\nsys.exit(1)\n" if failed else
            "print('================== tests finished: 0 passed, in 0.00 seconds ===================')\n")
        for argv in (["init", "-q"], ["add", "."], ["-c", "user.name=Offline", "-c", "user.email=offline@example.invalid",
                                                 "commit", "-qm", "public negative test"]):
            subprocess.run(["git", *argv], cwd=source, check=True, capture_output=True)
        instance.update(instance_id="fixture__native-test-1", repo="fixture/native-test",
                        base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
                        problem_statement="Diagnose the failing native test. Report remaining work honestly.")
    else:
        source = Path(os.environ["NZ_SWE_SYMPY_BASE"])
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() == instance["base_commit"]
        assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=source)
    bare = case / "workspace/.nz-coder/swebench-lite/repo-cache" / (instance["repo"].replace("/", "_") + ".git")
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    (case / "input/instances.json").write_text(json.dumps(packet))
    entry = (root / "tests/evaluation/fixtures/swebench_relay_entry.py").read_text()
    shutil.copy2(root / "tests/evaluation/fixtures/swebench_close_observer.py",
                 case / "driver/swebench_close_observer.py")
    # 无人值守 CLI 的工作区仍未信任；通过现有审批接口注入本轮已授权的窄测试，
    # 不修改 PermissionManager，也不把安全拒绝改成允许。
    entry = entry.replace("install_model_transport()\n\nif __name__", '''install_model_transport()

from nz_coder.runtime.execution import composition
from nz_coder.swebench.policy import strict_bash_violation
from nz_coder.intelligence.verification_planner import classify_verification_command
from nz_coder.runtime.process.workdir import current_workdir
import json
original_environment = composition.build_product_environment

def approved_test(name, arguments):
    command = str(arguments.get('command') or '')
    workspace = current_workdir().resolve()
    allowed = (workspace.parent == Path('/workspace/runs') and name == 'bash'
        and not strict_bash_violation(command)
        and classify_verification_command(command) in {'targeted', 'static'})
    with Path('/result/permissions.jsonl').open('a') as output:
        output.write(json.dumps(dict(tool=name,input=arguments,allowed=allowed,workspace=str(workspace))) + '\\n')
    return allowed

def configured_environment(system_prompt, **kwargs):
    return original_environment(system_prompt, permission_asker=approved_test, **kwargs)

composition.build_product_environment = configured_environment

if __name__''')
    entry = entry.replace('if __name__ == "__main__":', '''from swebench_close_observer import install as observe_close
observe_close()

if __name__ == "__main__":''')
    # 只读的环境回执在真实 CLI 结束后产生，不修改项目、不向模型提供解题分析。
    entry = entry.replace("raise SystemExit(main(sys.argv[1:]))", """result = main(sys.argv[1:])
    import json, subprocess
    workspace = Path('/workspace/runs') / json.loads(Path('/input/instances.json').read_text())['instances'][0]['instance_id']
    probe = subprocess.run(['/opt/miniconda3/envs/testbed/bin/python', '-c',
        'import sys,json,importlib.util; import mpmath; '
        'print(json.dumps(dict(executable=sys.executable,version=sys.version,mpmath=mpmath.__version__, '
        'sympy_origin=getattr(importlib.util.find_spec("sympy"),"origin",None))))'],
        cwd=workspace, capture_output=True, text=True, check=True)
    Path('/result/environment.json').write_text(json.dumps(dict(agent_executable=sys.executable,
        agent_version=sys.version,project=json.loads(probe.stdout))))
    raise SystemExit(result)""")
    (case / "driver/entry.py").write_text(entry)
    requests, responses, errors = [], [], []

    class Boundary(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            requests.append(request)
            names = [tool["function"]["name"] for tool in request.get("tools", [])]
            is_main = "bash" in names
            try:
                if is_main:
                    assert "python3 bin/test" in json.dumps(request["messages"])
                    assert "/opt/miniconda3/envs/testbed/bin/python3" in json.dumps(request["messages"])
                    previous = [m for m in request["messages"] if m["role"] == "tool"]
                    call_names = {c["id"]: c["function"]["name"] for m in request["messages"] for c in m.get("tool_calls", [])}
                    bash_results = [m for m in previous if call_names.get(m["tool_call_id"]) == "bash"]
                    if not previous:
                        tool, arguments = "read_file", {"path": "bin/test" if negative else "bin/get_sympy.py"}
                    elif not bash_results:
                        tool, arguments = "bash", {"command": "python3 bin/test test_sympify --no-colors", "timeout": 30}
                    else:
                        text = json.dumps(bash_results)
                        if failed:
                            assert "Command exited with code 1" in text
                            assert "controlled negative test" in text
                        elif negative:
                            assert "tests finished: 0 passed" in text
                            assert "Command exited with code" not in text
                        else:
                            assert "45 passed" in text and "5 skipped" in text and "2 expected to fail" in text
                            assert "3.9.20" in text
                        tool, arguments = None, None
                    if tool:
                        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "local-" + str(len(requests)),
                            "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
                    else:
                        message = {"role": "assistant", "content": "Health check only; no issue fix was implemented. Remaining repair is unverified."}
                elif "emit_sidecar_verdict" in names:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "local-review",
                        "type": "function", "function": {"name": "emit_sidecar_verdict", "arguments": json.dumps(
                            {"verdict": "revise", "reason": "Only health channel tested; issue not repaired."})}}]}
                else:
                    message = {"role": "assistant", "content": "Health check only, no repair, no completion evidence."}
                body = json.dumps({"id": "local-native-test", "object": "chat.completion", "created": 0,
                    "model": request["model"], "choices": [{"index": 0, "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                    "usage": {"prompt_tokens": len(raw), "completion_tokens": 4, "total_tokens": len(raw) + 4}}).encode()
                responses.append(json.loads(body))
                self.send_response(200)
            except AssertionError as exc:
                errors.append(str(exc))
                body = json.dumps({"error": "local protocol assertion"}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    def counter(raw, _request):
        return InputAccounting(len(raw), "local-json-byte-test-count", trusted=True, exact=True,
                               evidence_level="exact", contract_id="local-only", payload_sha256=hashlib.sha256(raw).hexdigest())

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Boundary)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    ledger = RelayLedger(case / "ledger.jsonl", RelayLimits(requests=6, input_total=500000, output_total=60000))
    servers = []
    for role in ("main", "auxiliary"):
        server = RelayServer(case / "sockets" / (role + ".sock"), ledger=ledger,
            binding=RelayBinding("offline-native", instance["instance_id"], case.name, role, 3,
                "deepseek-v4-flash", 8000, time.monotonic() + 90, role == "main"),
            upstream=f"http://127.0.0.1:{fake.server_port}/v1/chat/completions", counter=counter,
            trace_directory=case / "private-provider")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    argv = launcher.launch_argv(tmp_path, case, image=image)
    argv.insert(2, "--pull=never")
    index = argv.index(image)
    argv[index:index] = ["--env", "MAX_AGENT_TURNS=3", "--env", "NZ_SWE_NOMINAL_AGENT_TURNS=3",
                         "--env", "MAX_OUTPUT_TOKENS=8000", "--env", "NZ_PROVIDER_MAX_RETRIES=0"]
    argv[argv.index(image) + 1] = "/usr/local/bin/python"
    argv += ["run-agent", "--profile", "verified", "--instances-file", "/input/instances.json",
        "--run-id", case.name, "--work-root", "/workspace/runs", "--output", "/result/predictions.jsonl",
        "--clone-timeout", "15", "--agent-timeout", "60", "--no-cleanup-worktrees",
        # SymPy 的已有事务快照约 40 MiB，不能套用微型项目的 20 MiB 归档配额。
        "--trace-budget-gib", "0.1", "--trace-warning-gib", "0.08", "--trace-cleanup-target-gib", "0.06"]
    (case / "launch.json").write_text(json.dumps({"argv": argv, "image": json.loads(inspect.stdout)[0]["Id"]}, indent=2))
    try:
        # 外层还包括近两千个事务快照文件的已有归档；Agent 仍限 60s，Bash 仍限 30s。
        try:
            completed = subprocess.run(argv, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                                       capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            for name, value in (("stdout", exc.stdout), ("stderr", exc.stderr)):
                (case / (name + ".txt")).write_bytes(value.encode() if isinstance(value, str) else value or b"")
            raise
        (case / "stdout.txt").write_text(completed.stdout)
        (case / "stderr.txt").write_text(completed.stderr)
        (case / "provider-requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in requests))
        (case / "provider-responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in responses))
        assert not errors, errors
        assert completed.returncode in {0, 1}, completed.stdout + completed.stderr
        environment = json.loads((case / "result/environment.json").read_text())
        assert environment["agent_executable"] == "/usr/local/bin/python"
        assert environment["agent_version"].startswith("3.12.")
        assert environment["project"]["version"].startswith("3.9.20")
        assert environment["project"]["mpmath"] == "1.3.0"
        if not negative:
            assert environment["project"]["sympy_origin"] == f"/workspace/runs/{instance['instance_id']}/sympy/__init__.py"
        workdir = case / "workspace/runs" / instance["instance_id"]
        facts = [json.loads(line) for line in (workdir / ".nz-coder-runs/execution-facts.jsonl").read_text().splitlines()]
        commands = [r["result"] for r in facts if r["event"] == "tool_execution_result" and r["result"]["name"] == "bash"]
        assert len(commands) == 1
        result = commands[0]
        assert result["executed"] and not result["dispatch_failed"]
        assert result["command_failed"] is failed
        assert result["metadata"]["exit"] == int(failed)
        assert result["metadata"]["requested_command"] == result["metadata"]["executed_command"] == "python3 bin/test test_sympify --no-colors"
        assert result["metadata"]["resolved_executable"] == "/opt/miniconda3/envs/testbed/bin/python3"
        traces = [json.loads(line) for path in (workdir / ".nz-coder-runs").glob("*.jsonl")
                  if path.name != "execution-facts.jsonl" for line in path.read_text().splitlines()]
        verification = [r for r in traces if r.get("event") == "verification_result"]
        expected = "failed" if failed else "skipped" if negative else "passed"
        assert any(r.get("stage") == "targeted" and r.get("status") == expected for r in verification), verification
        run_end = next(r for r in traces if r.get("event") == "run_end")
        assert run_end["last_verification"]["command"] == "python3 bin/test test_sympify --no-colors"
        assert run_end["last_verification"]["status"] == expected
        assert _collect_diff(workdir) == ""
        report = json.loads((case / "result/predictions.report.json").read_text())[0]
        assert report["official_resolved"] is None
        assert report["patch_status"] == "empty"
        # 原生回合可以结束；没有补丁/官方评分，不能据此声称 SWE 解题成功。
        assert report["agent_status"]["status"] == run_end["status"] == "completed"
        lifecycle = [json.loads(line) for line in (case / "result/lifecycle.jsonl").read_text().splitlines()]
        sequence = [row["event"] for row in lifecycle]
        for before, after in zip(("agent_execution_finished", "cleanup_started", "cleanup_finished", "worker_result_received"),
                                 ("cleanup_started", "cleanup_finished", "worker_result_received", "attempt_parent_returned")):
            assert sequence.index(before) < sequence.index(after)
        closed = next(row for row in lifecycle if row["event"] == "cleanup_finished")
        assert closed["complete"] and not closed["failures"]
        assert not any(t["name"].startswith(("nz-repo-", "nz-process")) for t in closed["threads"])
        assert next(row for row in lifecycle if row["event"] == "attempt_parent_returned")["children"] == []
        finished = next(row for row in lifecycle if row["event"] == "agent_execution_finished")
        ledger_rows = [json.loads(line) for line in (case / "ledger.jsonl").read_text().splitlines()]
        assert all(row["monotonic"] < finished["monotonic"] for row in ledger_rows if row["event"] == "admitted")
        assert not (case / "result/thread-stacks.txt").read_text()
        assert len(requests) <= 6
    finally:
        ledger.cancel(case.name)
        subprocess.run(["docker", "rm", "-f", "nz-review-" + case.name], capture_output=True)
        for server in servers:
            server.shutdown()
            server.server_close()
        fake.shutdown()
        fake.server_close()
        ledger.close()
