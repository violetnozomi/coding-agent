"""本轮单一Docker启动方式与宿主Unix预算转发的离线验收。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nz_coder.evaluation.model_relay import (  # noqa: E402
    InputAccounting, RelayBinding, RelayLedger, RelayLimits, RelayServer,
)

BASE = "python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
VERSIONS = {"old": "cf2ff5cf078559e9843c34614318d80c984b4168", "new": "4abbcafae69f2e3b9d162ad73e769913659aca59"}
FROZEN = ROOT / "docs/evidence/review-effects-preflight-2026-10-09"
FIXTURES = ROOT / "tests/evaluation/fixtures"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docker(*arguments, **kwargs):
    env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"}
    return subprocess.run(["docker", *arguments], env=env, check=True, text=True, capture_output=True, **kwargs)


def prepare_runtime(directory):
    assert not (directory / "runtime.json").exists(), "completed runtime preparation must not be overwritten"
    subprocess.run([sys.executable, str(FROZEN / "verify.py")], check=True)
    before = sha(FROZEN / "manifest.json")
    for label, commit in VERSIONS.items():
        bundle = directory / ("code-" + label)
        bundle.mkdir()
        archive = subprocess.check_output(["git", "archive", commit, "nz_coder"], cwd=ROOT)
        files = {}
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            for member in stream:
                path = Path(member.name)
                # 评分/基准代码不进入实例；保留原生产包（含既有swebench权限依赖）。
                if not member.isfile() or "evaluation" in path.parts or path.name in {"benchmark.py", "aider_benchmark.py", "eval_runner.py", "swebench_lite.py"}:
                    continue
                target = bundle / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.extractfile(member).read())
                files[str(path)] = sha(target)
        save(directory / ("code-" + label + "-hashes.json"), {"source_commit": commit, "files": files})
    build = directory / "build"
    build.mkdir()
    shutil.copytree(directory / "wheels", build / "wheels")
    wheel_hashes, requirements = {}, []
    for wheel in sorted((build / "wheels").glob("*.whl")):
        wheel_hashes[wheel.name] = sha(wheel)
        with zipfile.ZipFile(wheel) as stream:
            metadata = stream.read(next(name for name in stream.namelist() if name.endswith(".dist-info/METADATA"))).decode()
        name = next(line[6:] for line in metadata.splitlines() if line.startswith("Name: "))
        version = next(line[9:] for line in metadata.splitlines() if line.startswith("Version: "))
        requirements.append(f"{name}=={version} --hash=sha256:{sha(wheel)}")
    (build / "requirements.txt").write_text("\n".join(requirements) + "\n")
    (build / "Dockerfile").write_text(f"FROM {BASE}\nCOPY wheels /wheels\nCOPY requirements.txt /requirements.txt\nRUN python -m pip install --no-index --require-hashes --no-deps --find-links=/wheels -r /requirements.txt && rm -rf /wheels\n")
    tag = "nz-review-runtime:" + hashlib.sha256((build / "requirements.txt").read_bytes()).hexdigest()[:12]
    result = docker("build", "--network=none", "--pull=false", "-t", tag, str(build), timeout=180)
    (directory / "build.txt").write_text(result.stdout + result.stderr)
    image = docker("image", "inspect", tag, "--format", "{{.Id}}").stdout.strip()
    driver = directory / "driver"
    driver.mkdir()
    shutil.copyfile(FIXTURES / "review_execution_entry.py", driver / "entry.py")
    manifest = json.loads((FROZEN / "manifest.json").read_text())
    formal = directory / "formal"
    initial_hashes = {}
    for row in manifest["order"] + [{"unit": "a01", "version": "new"}]:
        source = FROZEN / ("prepared/autonomous/initial" if row["unit"] == "a01" else "prepared/gate/initial")
        target = formal / row["unit"] / "workspace"
        shutil.copytree(source, target)
        expected = manifest["autonomous"]["initial_hashes"] if row["unit"] == "a01" else manifest["gate"]["initial_hashes"]
        assert {name: sha(target / name) for name in expected} == expected
        initial_hashes[row["unit"]] = expected
    assert before == sha(FROZEN / "manifest.json")
    save(directory / "runtime.json", {"image": image, "base": BASE, "versions": VERSIONS,
        "original_manifest_sha256": before, "wheel_hashes": wheel_hashes,
        "driver_sha256": sha(driver / "entry.py"), "formal_initial_hashes": initial_hashes,
        "python": "3.12.13 (both Core versions share this runtime; previous preflight was 3.13)",
        "dependencies": requirements})
    print(json.dumps({"runtime_prepared": True, "image": image, "real_model_requests": 0}))


def launch_argv(directory, case, *, image):
    name = "nz-review-" + case.name
    command = ["docker", "run", "--name", name, "--init", "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--ipc=private", "--cgroupns=private", "--pids-limit=128",
               "--memory=512m", "--user=1001:1001", "--workdir=/workspace", "--label", "nz-review-case=" + case.name]
    for source, target, readonly in ((directory / "code-new", "/runtime", True), (case / "driver", "/driver", True),
                                   (case / "workspace", "/workspace", False), (case / "home", "/home/agent", False),
                                   (case / "tmp", "/tmp", False), (case / "input", "/input", True),
                                   (case / "sockets", "/model", True), (case / "result", "/result", False),
                                   (case / "control", "/control", True)):
        command += ["--mount", f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")]
    for key, value in {"HOME": "/home/agent", "TMPDIR": "/tmp", "PYTHONPATH": "/runtime", "LANG": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0"}.items():
        command += ["--env", key + "=" + value]
    return command + [image, "python", "/driver/entry.py"]


def local_counter(raw, _request):
    return InputAccounting(len(raw), "local-fake-json-byte-tokens-only", trusted=True, exact=True)


def probe(directory, probe_id, modes):
    runtime = json.loads((directory / "runtime.json").read_text())
    spec = importlib.util.spec_from_file_location("execution_fake", FIXTURES / "review_execution_fake.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for label in ("old", "new"):
        expected = json.loads((directory / ("code-" + label + "-hashes.json")).read_text())["files"]
        assert {name: sha(directory / ("code-" + label) / name) for name in expected} == expected
    receipts = []
    for mode in modes:
        case = directory / ("probe-" + mode + "-" + probe_id)
        assert not case.exists(), "retain previous probe; no automatic overwrite/retry"
        for name in ("workspace", "home", "tmp", "input", "sockets", "result", "control", "private", "driver"):
            (case / name).mkdir(parents=True)
        shutil.copyfile(FIXTURES / "review_execution_entry.py", case / "driver/entry.py")
        private = case / "private/sentinel.txt"
        private.write_text("nonsensitive isolation sentinel\n")
        for name in ("score", "other-candidate", "host-home", "credentials"):
            (case / "private" / name).write_text("nonsensitive sentinel\n")
        ws = case / "workspace"
        fake = module.ExecutionFake(cancel=mode != "positive")
        fake_thread = threading.Thread(target=fake.serve_forever, daemon=True)
        fake_thread.start()
        (ws / "calc.py").write_text("value = 1\n")
        (ws / "tests").mkdir()
        (ws / "tests/test_calc.py").write_text("from calc import value\ndef test_value(): assert value == 2\n")
        shutil.copyfile(FIXTURES / "review_boundary_probe.py", ws / "boundary_probe.py")
        (ws / "outside-link").symlink_to(private)
        targets = {"absolute": str(private), "score": str(case / "private/score"),
                   "other_candidate": str(case / "private/other-candidate"), "host_home": str(case / "private/host-home"),
                   "credential_sentinel": str(case / "private/credentials"), "host_evidence": str(FROZEN / "README.md")}
        save(ws / "probe-config.json", {"outside_targets": targets, "host_pid": os.getpid(), "host_port": fake.server_port})
        (ws / "heartbeat.py").write_text('''import os,subprocess,sys,time,uuid
from pathlib import Path
import httpx
if "--child" not in sys.argv: subprocess.Popen([sys.executable,__file__,"--child"])
p=Path("heartbeat-child.txt" if "--child" in sys.argv else "heartbeat-parent.txt")
with httpx.Client(transport=httpx.HTTPTransport(uds="/model/main.sock"),trust_env=False,timeout=1) as c:
 while True:
  with p.open("a") as f: f.write("tick\\n")
  try: c.post("http://localhost/v1/chat/completions",headers={"X-NZ-Call-Id":uuid.uuid4().hex},json={"model":"deepseek-v4-flash","max_tokens":64000,"messages":[{"role":"user","content":"heartbeat"}],"stream":False})
  except httpx.HTTPError: pass
  time.sleep(0.03)
''')
        commands = ["python boundary_probe.py", "python -m pytest -q tests"] if mode == "positive" else ["python heartbeat.py"]
        task = ("Set calc.py value to 2. Preserve integer result behavior. Verify workspace boundaries using the existing probe script. Run python -m pytest -q tests and report actual results."
                if mode == "positive" else "Run python heartbeat.py to exercise cancellation. Do not edit code.")
        save(case / "input/run.json", {"task": task, "bash_commands": commands})
        ledger = RelayLedger(case / "ledger.jsonl", RelayLimits())
        run_seconds = 6 if mode == "timeout" else 30
        deadline = time.monotonic() + run_seconds
        servers, threads = [], []
        for role, cap, output, exact in (("main", 12, 64000, True), ("auxiliary", 6, 64000, False)):
            binding = RelayBinding("execution-probe", mode, mode, role, cap, "deepseek-v4-flash", output, deadline, exact)
            server = RelayServer(case / "sockets" / (role + ".sock"), ledger=ledger, binding=binding,
                upstream=f"http://127.0.0.1:{fake.server_port}/v1/chat/completions", counter=local_counter)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        argv = launch_argv(directory, case, image=runtime["image"])
        container_name = "nz-review-" + case.name
        assert container_name not in docker("ps", "-a", "--format", "{{.Names}}").stdout.splitlines(), "existing container must not be replaced"
        with ExitStack() as stack:
            sentinel_fd = stack.enter_context(private.open())
            os.set_inheritable(sentinel_fd.fileno(), True)
            stdout = stack.enter_context((case / "stdout.txt").open("w"))
            stderr = stack.enter_context((case / "stderr.txt").open("w"))
            env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"}
            process = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr, close_fds=True)

            def stop_active_container():
                if process.poll() is None:
                    ledger.cancel(mode)
                    docker("kill", container_name)
                    process.wait(timeout=5)

            # 宿主启动器本身异常/取消时也收掉容器，不能留下后台工具后代。
            stack.callback(stop_active_container)
            started = time.monotonic()
            control_sent = False
            timed_out = False
            while process.poll() is None:
                if mode == "cancel" and not control_sent and (ws / "heartbeat-child.txt").exists():
                    ledger.cancel(mode)
                    (case / "control/cancel").write_text("cancel")
                    control_sent = True
                if time.monotonic() >= deadline:
                    ledger.cancel(mode)
                    docker("kill", "nz-review-" + case.name)
                    timed_out = True
                    break
                threading.Event().wait(0.02)
            process.wait(timeout=5)
        inspected = json.loads(docker("inspect", "nz-review-" + case.name).stdout)[0]
        assert inspected["Config"]["Labels"].get("nz-review-case") == case.name
        save(case / "container-inspect.json", inspected)
        save(case / "launch.json", {"argv": argv, "environment_whitelist": env})
        docker("rm", "nz-review-" + case.name)
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
        fake.shutdown()
        fake.server_close()
        fake_thread.join(2)
        before_late = {p.name: sha(p) for p in ws.glob("heartbeat-*.txt")}
        calls = len(fake.requests)
        threading.Event().wait(0.15)
        assert before_late == {p.name: sha(p) for p in ws.glob("heartbeat-*.txt")} and calls == len(fake.requests)
        assert not inspected["State"]["Running"]
        save(case / "fake-requests.json", fake.requests)
        state = json.loads((case / "result/result.json").read_text()) if (case / "result/result.json").exists() else None
        receipt = {"mode": mode, "probe_id": probe_id, "driver_sha256": sha(case / "driver/entry.py"),
            "exit_code": process.returncode, "runtime_result": state,
            "elapsed": time.monotonic() - started, "timed_out": timed_out,
            "descendant_writes_stopped": True, "late_requests": 0, "fake_upstream_requests": calls,
            "physical_attempts": len(ledger.attempts), "occupied_input": ledger.input_occupied,
            "occupied_output": ledger.output_occupied, "sentinels_unchanged": private.read_text() == "nonsensitive isolation sentinel\n"
                and all((case / "private" / name).read_text() == "nonsensitive sentinel\n"
                        for name in ("score", "other-candidate", "host-home", "credentials")),
            "host_config": {k: inspected["HostConfig"][k] for k in ("NetworkMode", "ReadonlyRootfs", "CapDrop", "Privileged", "PidMode", "IpcMode", "CgroupnsMode")},
            "mounts": [{"destination": x["Destination"], "writable": x["RW"]} for x in inspected["Mounts"]]}
        if mode == "positive":
            assert state["status"] == "completed", (state, (case / "stderr.txt").read_text())
            assert (ws / "calc.py").read_text() == "value = 2\n"
            trace = [json.loads(line) for line in (case / "result/runtime.jsonl").read_text().splitlines()]
            assert any(e.get("event") == "verification_result" and e.get("status") == "passed" for e in trace)
            tool = next(e for e in trace if e.get("event") == "tool_call" and e.get("input", {}).get("command") == "python boundary_probe.py")
            receipt["boundary_tool_result"] = tool
            assert '"passed": true' in str(tool)
            assert any(a["role"] == "auxiliary" for a in ledger.attempts.values())
        elif mode == "cancel":
            assert control_sent
            trace = [json.loads(line) for line in (case / "result/runtime.jsonl").read_text().splitlines()]
            assert any(e.get("event") == "run_end" and e.get("status") == "cancelled" for e in trace)
            if state is not None:
                assert state["status"] == "cancelled"
            else:
                cancellation = json.loads((case / "result/cancellation.json").read_text())
                assert cancellation == {"exception": "CancelledError", "run_result": None}
                receipt["runtime_exception"] = cancellation
            receipt["runtime_end_status"] = "cancelled"
        else:
            assert timed_out and before_late and process.returncode != 0
        ledger.close()
        save(case / "receipt.json", receipt)
        receipts.append(receipt)
        print(json.dumps({"probe": mode, "status": state["status"] if state else receipt.get("runtime_end_status", "deadline_killed"), "requests": calls}))
    save(directory / ("probe-results-" + probe_id + ".json"), receipts)


def verify(directory, selected):
    runtime = json.loads((directory / "runtime.json").read_text())
    assert sha(FROZEN / "manifest.json") == runtime["original_manifest_sha256"]
    for label in VERSIONS:
        inventory = json.loads((directory / ("code-" + label + "-hashes.json")).read_text())
        assert inventory["source_commit"] == VERSIONS[label]
        assert {str(p.relative_to(directory / ("code-" + label))): sha(p)
                for p in (directory / ("code-" + label)).rglob("*") if p.is_file()} == inventory["files"]
    receipts = {}
    for mode, probe_id in selected.items():
        case = directory / ("probe-" + mode + "-" + probe_id)
        path = case / "receipt.json"
        receipt = json.loads(path.read_text())
        assert receipt["mode"] == mode and receipt["probe_id"] == probe_id
        assert sha(case / "driver/entry.py") == receipt["driver_sha256"] == sha(FIXTURES / "review_execution_entry.py")
        assert receipt["descendant_writes_stopped"] and receipt["late_requests"] == 0 and receipt["sentinels_unchanged"]
        inspected = json.loads((case / "container-inspect.json").read_text())
        hc = inspected["HostConfig"]
        assert hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"]
        assert not hc["Privileged"] and hc["PidMode"] == "" and hc["IpcMode"] == "private"
        assert "no-new-privileges" in hc["SecurityOpt"]
        assert inspected["Image"] == runtime["image"] and inspected["Config"]["User"] == "1001:1001"
        assert not inspected["State"]["Running"]
        allowed = {"/runtime": (directory / "code-new", False), "/driver": (case / "driver", False),
                   "/input": (case / "input", False), "/model": (case / "sockets", False),
                   "/control": (case / "control", False), "/workspace": (case / "workspace", True),
                   "/home/agent": (case / "home", True), "/tmp": (case / "tmp", True), "/result": (case / "result", True)}
        assert {m["Destination"]: (Path(m["Source"]), m["RW"]) for m in inspected["Mounts"]} == allowed
        if mode == "positive":
            assert receipt["runtime_result"]["status"] == "completed"
            assert receipt["exit_code"] == 0 and '"passed": true' in str(receipt["boundary_tool_result"])
            assert sha(case / "workspace/boundary_probe.py") == sha(FIXTURES / "review_boundary_probe.py")
        elif mode == "cancel":
            assert receipt["runtime_end_status"] == "cancelled" and not receipt["timed_out"]
        else:
            assert receipt["timed_out"] and receipt["exit_code"] != 0 and receipt["runtime_result"] is None
        admissions = [json.loads(line) for line in (case / "ledger.jsonl").read_text().splitlines()]
        admitted = [r for r in admissions if r["event"] == "admitted"]
        assert len(admitted) == receipt["physical_attempts"] == len(json.loads((case / "fake-requests.json").read_text()))
        receipts[mode] = {"case": case.name, "receipt_sha256": sha(path),
                          "ledger_sha256": sha(case / "ledger.jsonl"),
                          "inspect_sha256": sha(case / "container-inspect.json")}
    assert set(receipts) == {"positive", "cancel", "timeout"}
    save(directory / "isolation-verified.json", {"isolation_verified": True, "runtime_sha256": sha(directory / "runtime.json"),
         "receipts": receipts, "real_provider_requests": 0})
    print(json.dumps({"isolation_verified": True, "real_provider_requests": 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-runtime", "probe", "verify", "status", "online"))
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--probe-id", default="initial")
    parser.add_argument("--probe-modes", nargs="+", choices=("positive", "cancel", "timeout"), default=("positive", "cancel", "timeout"))
    parser.add_argument("--positive-probe-id")
    parser.add_argument("--cancel-probe-id")
    parser.add_argument("--timeout-probe-id")
    args = parser.parse_args()
    directory = args.artifact_root.resolve()
    if args.action == "prepare-runtime":
        prepare_runtime(directory)
    elif args.action == "probe":
        if not args.probe_id.isascii() or not args.probe_id.replace("-", "").isalnum():
            parser.error("probe-id must contain only ASCII letters, digits and hyphens")
        probe(directory, args.probe_id, args.probe_modes)
    elif args.action == "verify":
        verify(directory, {mode: getattr(args, mode + "_probe_id") or args.probe_id for mode in ("positive", "cancel", "timeout")})
    else:
        verified_path = directory / "isolation-verified.json"
        isolation = False
        if verified_path.exists():
            verified = json.loads(verified_path.read_text())
            isolation = verified["isolation_verified"] and sha(directory / "runtime.json") == verified["runtime_sha256"]
            for row in verified["receipts"].values():
                case = directory / row["case"]
                isolation = isolation and sha(case / "receipt.json") == row["receipt_sha256"] \
                    and sha(case / "ledger.jsonl") == row["ledger_sha256"] \
                    and sha(case / "container-inspect.json") == row["inspect_sha256"] \
                    and sha(case / "driver/entry.py") == sha(FIXTURES / "review_execution_entry.py")
            runtime = json.loads((directory / "runtime.json").read_text())
            isolation = isolation and sha(FROZEN / "manifest.json") == runtime["original_manifest_sha256"]
            for label in VERSIONS:
                inventory = json.loads((directory / ("code-" + label + "-hashes.json")).read_text())["files"]
                isolation = isolation and all(sha(directory / ("code-" + label) / name) == value for name, value in inventory.items())
        ready = {"isolation_verified": isolation,
                 "budget_enforcement_verified": False, "paid_authorization_valid": False,
                 "technical_ready": False, "online_not_run": True,
                 "blockers": ["selected DeepSeek model has no validated input tokenizer/upper bound", "missing_paid_authorization"]}
        print(json.dumps(ready))
        if args.action == "online":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
