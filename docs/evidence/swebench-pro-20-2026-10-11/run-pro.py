"""本轮固定 20 题的执行命令；复用生产 SDK、既有转发账本与官方 verifier。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ARTIFACTS = Path("/home/pyh/.codex/artifacts")
OUT = ARTIFACTS / "nz-swebench-pro-v2-20-2026-10-11"
DOC = Path(__file__).resolve().parent
ENV = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"}
MODEL = "deepseek-flash"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def command(argv, *, log=None, timeout=60, check=True):
    if log:
        with log.open("w") as sink:
            r = subprocess.run(argv, env=ENV, stdout=sink, stderr=subprocess.STDOUT, timeout=timeout)
    else:
        r = subprocess.run(argv, env=ENV, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"command failed ({r.returncode}): {argv[:3]}" + ("" if log else r.stderr[-2000:]))
    return r


def download(relative, manifest, checksums):
    url = f'https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/{manifest["official_source_commit"]}/v2/{relative}'
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=12) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except Exception:
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != checksums[relative]:
        raise RuntimeError("official file checksum mismatch")
    return raw


def registry_preflight(image, slot):
    """只读公开层尺寸；空间估计不是镜像解压大小的精确保证。"""
    repository, tag = image.removeprefix("ghcr.io/").split(":", 1)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:" + repository + ":pull"
    with opener.open(token_url, timeout=20) as response:
        token = json.load(response)["token"]
    def fetch(reference):
        request = urllib.request.Request("https://ghcr.io/v2/" + repository + "/manifests/" + reference,
            headers={"Authorization": "Bearer " + token, "Accept": ", ".join([
                "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json"])})
        with opener.open(request, timeout=30) as response:
            return json.load(response), response.headers.get("Docker-Content-Digest")
    value, index_digest = fetch(tag)
    digest = index_digest
    if "manifests" in value:
        selected = [item for item in value["manifests"]
                    if item.get("platform", {}).get("os") == "linux"
                    and item.get("platform", {}).get("architecture") == "amd64"]
        if len(selected) != 1:
            raise RuntimeError("image_platform_selection_ambiguous")
        value, digest = fetch(selected[0]["digest"])
    compressed = sum(layer["size"] for layer in value["layers"])
    free = shutil.disk_usage(OUT).free
    estimate = int(compressed * 3.5) + 1024 ** 3
    save(slot / "image-preflight.json", {"image": image, "digest": digest, "index_digest": index_digest, "compressed_bytes": compressed,
        "estimated_required_bytes": estimate, "free_bytes": free, "estimate_is_not_exact": True})
    if free < estimate:
        raise RuntimeError("insufficient_storage_for_image_and_retained_evidence")


def runtime_image(row, slot):
    image = row["configuration"]["environment"]["docker_image"]
    available = command(["docker", "image", "inspect", image], check=False)
    if available.returncode:
        registry_preflight(image, slot)
        command(["docker", "pull", image], log=slot / "image-pull.txt", timeout=420)
    info = json.loads(command(["docker", "image", "inspect", image]).stdout)[0]
    base = info["RepoDigests"][0]
    python_version = command(["docker", "run", "--rm", "--network=none", "--entrypoint", "python3",
                       base, "--version"]).stdout.strip().split()[1]
    minor = "".join(python_version.split(".")[:2])
    build = OUT / "runtime" / ("python-" + minor)
    if not build.exists():
        build.mkdir()
        wheels = build / "wheels"
        if minor == "311":
            shutil.copytree(OUT / "runtime/wheels", wheels)
        else:
            command([sys.executable, "-m", "pip", "download", "--no-cache-dir", "--no-deps", "--only-binary=:all:",
                     "--python-version", minor, "--implementation", "cp", "--abi", "cp" + minor,
                     "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_17_x86_64",
                     "--platform", "manylinux2014_x86_64", "-r", str(OUT / "runtime/requirements-pinned.txt"),
                     "-d", str(wheels)], log=build / "download.txt", timeout=180)
        requirements = []
        for p in sorted(wheels.glob("*.whl")):
            with zipfile.ZipFile(p) as z:
                m = z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))).decode()
            name = next(x[6:] for x in m.splitlines() if x.startswith("Name: "))
            version = next(x[9:] for x in m.splitlines() if x.startswith("Version: "))
            requirements.append(f"{name}=={version} --hash=sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}")
        (build / "requirements.txt").write_text("\n".join(requirements) + "\n")
    # 构建只添加隔离 Agent 的依赖，不改变官方复评镜像。
    (build / "Dockerfile").write_text(f"FROM {base}\nCOPY wheels /nz-wheels\nCOPY requirements.txt /nz-requirements.txt\n"
        "RUN python3 -m pip install --no-index --no-deps --require-hashes --target=/opt/nz-site --find-links=/nz-wheels -r /nz-requirements.txt && rm -rf /nz-wheels\n")
    tag = "nz-pro20-" + slot.name.lower() + ":20261011"
    command(["docker", "build", "--network=none", "--pull=false", "-t", tag, str(build)],
            log=slot / "image-build.txt", timeout=240)
    save(slot / "environment.json", {"source_image": base, "source_image_id": info["Id"], "project_python": python_version,
        "source_size_bytes": info["Size"], "source_created_for_this_round": True,
        "agent_image": tag, "agent_image_id": command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).stdout.strip()})
    return base, tag


def prepare_case(slot, image, row, smoke):
    case = slot / "case"
    for name in ("workspace", "home", "tmp", "input", "sockets", "result", "control", "driver"):
        (case / name).mkdir(parents=True)
    exporter = "nz-pro20-export-" + slot.name
    command(["docker", "create", "--name", exporter, "--entrypoint", "/bin/sh", image])
    try:
        repo = command(["docker", "run", "--rm", "--network=none", "--entrypoint", "/bin/sh", image,
                        "-c", "test -d /app/.git && echo /app || echo /testbed"]).stdout.strip()
        command(["docker", "cp", exporter + ":" + repo + "/.", str(case / "workspace")], timeout=180)
    finally:
        command(["docker", "rm", exporter])
    workspace = case / "workspace"
    image_env = json.loads(command(["docker", "image", "inspect", image]).stdout)[0]["Config"]["Env"]
    image_path = next(v.split("=", 1)[1] for v in image_env if v.startswith("PATH="))
    # 生产 Bash 使用 login shell；只在本题隔离 HOME 恢复镜像的工具链路径。
    (case / "home/.bash_profile").write_text("export PATH=" + __import__("shlex").quote(image_path) + "\n")
    goenv = case / "home/.config/go/env"
    goenv.parent.mkdir(parents=True)
    goenv.write_text("GOCACHE=/tmp/go-build-cache\nGOPROXY=off\nGOTOOLCHAIN=local\n")
    revision = command(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
    tracked = command(["git", "-C", str(workspace), "ls-files", "-z"]).stdout.split("\0")
    hashes = {}
    for name in filter(None, tracked):
        path = workspace / name
        if path.is_symlink():
            hashes[name] = {"symlink": os.readlink(path)}
        elif path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(slot / "initial-files.json", {"base_commit": revision, "repo_path_in_image": repo, "files": hashes})
    task = "Read go.mod and obtain go version. Report the observed version; do not modify files." if smoke else (
        (OUT / "public-tasks" / row["instance_id"] / "instruction.md").read_text()
        + "\n\nExecution environment: repository is /workspace. Network is unavailable during this attempt. "
          "Use the installed project toolchain and cached dependencies. Implement the requested change and verify it with relevant local tests.")
    save(case / "input/run.json", {"task": task, "model": MODEL, "session_id": "pro20-" + slot.name,
                                   "max_turns": 3 if smoke else 40})
    shutil.copy2(DOC / "native-entry.py", case / "driver/entry.py")
    return case


def run_case(slot, case, image, ledger, counter, *, key=None, upstream=None, smoke=False, agent_python="python3"):
    from nz_coder.evaluation.model_relay import RelayBinding, RelayServer
    servers = []
    name = "nz-pro20-agent-" + slot.name
    default_output = json.loads((case / "input/run.json").read_text()).get("provider_default_output", False)
    authorization = ledger.empirical_authorization
    deadline = time.monotonic() + (900 if smoke else 3000)
    try:
        for role, cap in (("main", 4 if smoke else 40), ("auxiliary", 4 if smoke else 6)):
            server = RelayServer(case / "sockets" / (role + ".sock"), ledger=ledger,
                binding=RelayBinding(authorization["purpose"] if authorization else "local-smoke",
                    slot_task(slot) if authorization else slot.name,
                    slot.name, role, cap, MODEL, 393216 if default_output else 8000, deadline, False),
                upstream=upstream or "https://api.deepseek.com/v1/chat/completions", counter=counter,
                remote_authorized=not smoke, api_key=key, trace_directory=slot / "private-provider")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        argv = ["docker", "run", "--name", name, "--init", "--pull=never", "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=4g",
                "--cpus=2", "--user=1001:1001", "--workdir=/workspace"]
        for src, target, readonly in ((OUT / "code", "/runtime", True), (case / "driver", "/driver", True),
                (case / "workspace", "/workspace", False), (case / "home", "/home/agent", False),
                (case / "tmp", "/tmp", False), (case / "input", "/input", True),
                (case / "sockets", "/model", True), (case / "result", "/result", False)):
            argv += ["--mount", f"type=bind,source={src},target={target}" + (",readonly" if readonly else "")]
        for k, v in {"HOME": "/home/agent", "TMPDIR": "/tmp", "PYTHONPATH": "/runtime:/opt/nz-site",
                "PYTHONDONTWRITEBYTECODE": "1", "MODEL_ID": MODEL, "GOENV": "/home/agent/.config/go/env", "MAX_AGENT_TURNS": "3" if smoke else "40",
                "MAX_OUTPUT_TOKENS": "65536" if default_output else "8000", "NZ_PROVIDER_MAX_RETRIES": "0", "GOCACHE": "/tmp/go-build-cache",
                "GOPROXY": "off", "GOTOOLCHAIN": "local", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}.items():
            argv += ["--env", k + "=" + v]
        if default_output:
            argv += ["--env", "MAX_CONTEXT_TOKENS=1000000"]
        argv += [image, agent_python, "/driver/entry.py"]
        save(slot / "launch.json", argv)
        start = time.monotonic()
        status = {}
        with (slot / "agent-stdio.txt").open("w") as sink:
            process = subprocess.Popen(argv, env=ENV, stdout=sink, stderr=subprocess.STDOUT)
            while process.poll() is None:
                pressure = shutil.disk_usage(OUT).free < 512 * 1024 ** 2
                expired = time.monotonic() > deadline + 60
                if pressure or expired:
                    ledger.cancel(slot.name)
                    command(["docker", "rm", "-f", name], check=False)
                    process.terminate()
                    status["outer_status"] = "storage_stop" if pressure else "timeout"
                    break
                time.sleep(1)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            status.update(returncode=process.returncode, duration_seconds=time.monotonic() - start)
        save(slot / "agent-exit.json", status)
    finally:
        ledger.cancel(slot.name)
        # 先终止整个容器的工具后代，才冻结宿主绑定的工作区。
        command(["docker", "rm", "-f", name], check=False)
        for s in servers:
            s.shutdown()
            s.server_close()


def slot_task(slot):
    return json.loads((slot / "identity.json").read_text())["instance_id"]


def freeze(slot, case):
    workspace = case / "workspace"
    original_revision = json.loads((slot / "initial-files.json").read_text())["base_commit"]
    command(["git", "-C", str(workspace), "-c", "core.hooksPath=/dev/null", "add", "-A", "--", ".",
             ":(exclude).nz-coder", ":(exclude).nz-coder-runs"])
    patch = command(["git", "-C", str(workspace), "diff", "--cached", "--binary", "--no-ext-diff"]).stdout.encode()
    (slot / "model.patch").write_bytes(patch)
    paths = command(["git", "-C", str(workspace), "diff", "--cached", "--name-only", "-z"]).stdout.split("\0")
    for name in filter(None, paths):
        p = workspace / name
        initial = command(["git", "-C", str(workspace), "show", original_revision + ":" + name], check=False)
        if initial.returncode == 0:
            before = slot / "initial-changed-files" / name
            before.parent.mkdir(parents=True, exist_ok=True)
            before.write_text(initial.stdout)
        if p.is_file() and not p.is_symlink():
            target = slot / "final-files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    save(slot / "patch-identity.json", {"sha256": hashlib.sha256(patch).hexdigest(), "bytes": len(patch),
                                      "status": "present" if patch else "empty", "frozen_after_container_stop": True})
    return patch


def grade(slot, base, row, manifest, checksums):
    verifier = OUT / "evaluator-only" / row["instance_id"] / "tests"
    verifier.mkdir(parents=True, exist_ok=True)
    prefix = "tasks/" + row["instance_id"] + "/tests/"
    for relative in checksums:
        if relative.startswith(prefix):
            p = verifier / relative.removeprefix(prefix)
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(download(relative, manifest, checksums))
            if hashlib.sha256(p.read_bytes()).hexdigest() != checksums[relative]:
                raise RuntimeError("official verifier changed")
    logs = slot / "evaluation"
    logs.mkdir()
    logs.chmod(0o777)
    name = "nz-pro20-grade-" + slot.name
    argv = ["docker", "run", "--name", name, "--init", "--pull=never", "--memory=4g", "--cpus=2",
        "--env", "HOME=/tmp/grade-home",
        "--mount", f"type=bind,source={verifier},target=/tests,readonly",
        "--mount", f"type=bind,source={slot / 'model.patch'},target=/tmp/model.patch,readonly",
        "--mount", f"type=bind,source={logs},target=/logs/verifier", "--entrypoint", "/bin/bash", base, "-c",
        "mkdir -p /tmp/grade-home; cd /app 2>/dev/null || cd /testbed; "
        "if [ -s /tmp/model.patch ]; then git apply --check /tmp/model.patch && git apply /tmp/model.patch || exit 97; fi; "
        "bash /tests/test.sh; result=$?; chmod -R a+rX /logs/verifier; exit $result"]
    save(slot / "evaluation-command.json", argv)
    try:
        r = command(argv, log=slot / "evaluation-stdio.txt", timeout=3060, check=False)
        reward = (logs / "reward.txt").read_text().strip() if (logs / "reward.txt").is_file() else None
        result = {"status": "graded" if reward in {"0", "1"} else "unknown", "returncode": r.returncode,
                  "reward": reward, "resolved": reward == "1" and r.returncode == 0 if reward in {"0", "1"} else None,
                  "fresh_original_image": base, "unchanged_official_verifier": True}
    except subprocess.TimeoutExpired:
        result = {"status": "timeout", "resolved": None}
    finally:
        command(["docker", "rm", "-f", name], check=False)
    save(slot / "evaluation-result.json", result)
    return result


def archive_case(slot, case, task, mode, budget, archive):
    workspace = case / "workspace"
    export = workspace / ".nz-coder/pro-export"
    export.mkdir(parents=True, exist_ok=True)
    shutil.copy2(case / "result/runtime.jsonl", export / "runtime.jsonl")
    shutil.copy2(case / "input/run.json", export / "run.json")
    archived = archive(instance_id=mode + "-" + task["instance_id"], workdir=workspace, run_root=case,
        trace_path=export / "runtime.jsonl", public_input_path=export / "run.json",
        metadata={"container_session_id": "pro20-" + slot.name,
                  "session_available_in_separate_container_archive": True}, budget=budget)
    # 容器 Session 原件中的 /workspace 保持不变；不改身份来迎合宿主归档校验。
    evidence_names = ("home/.local/state/nz-coder", "workspace/.nz-coder", "result", "input")
    evidence_bytes = sum(p.stat().st_size for name in evidence_names for p in (case / name).rglob("*")
                         if p.is_file() and not p.is_symlink())
    if evidence_bytes > 512 * 1024 ** 2 or shutil.disk_usage(OUT).free < evidence_bytes + 512 * 1024 ** 2:
        raise OSError("container_evidence_quota_or_free_space_exhausted; source retained")
    target = slot / "container-evidence.tar.gz"
    with tarfile.open(target, "x:gz", dereference=False) as bundle:
        for name in evidence_names:
            source = case / name
            if source.exists():
                bundle.add(source, arcname=name)
    with tarfile.open(target) as bundle:
        bundle.getmembers()
    save(slot / "archive-receipt.json", {"bundle": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size, "projected_trace_bundle": str(archived.bundle_path),
        "retains_raw_container_session_and_artifacts": True})
    # 补丁/改变文件已冻结，Session/Artifact 已成功归档，才能删除可重建工作区和缓存。
    for name in ("workspace", "tmp", "home"):
        shutil.rmtree(case / name)


def smoke_handler():
    class Handler(BaseHTTPRequestHandler):
        calls = 0
        def log_message(self, *_args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            n = Handler.calls
            if n == 2:
                last = payload["messages"][-1]
                if last.get("role") != "tool" or "go version go" not in str(last.get("content")):
                    self.send_error(502, "actual Go command did not deliver its version")
                    return
            Handler.calls += 1
            actions = [("read_file", {"path": "go.mod"}), ("bash", {"command": "go version"})]
            message = {"role": "assistant", "content": "Observed repository and installed Go version."}
            if n < len(actions):
                name, arguments = actions[n]
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"smoke-{n}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            usage = {"prompt_tokens": len(raw), "completion_tokens": 50, "total_tokens": len(raw) + 50}
            body = json.dumps({"id": "local-smoke", "object": "chat.completion", "created": 0, "model": payload["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if n < 2 else "stop"}], "usage": usage}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--smoke-id", default="offline-smoke", choices=["offline-smoke", "offline-smoke-2"])
    args = parser.parse_args()
    if args.smoke == args.execute:
        parser.error("choose exactly one of --smoke or --execute")
    from nz_coder.evaluation.model_relay import InputAccounting, RelayLedger, RelayLimits
    from nz_coder.swebench.trace_budget import TraceBudget, archive_instance_diagnostics
    manifest = json.loads((OUT / "task-manifest.json").read_text())
    checksums = {}
    for line in (OUT / "official-SHA256SUMS").read_text().splitlines():
        h, p = line.split(maxsplit=1); checksums[p.lstrip("*").removeprefix("./")] = h
    code = OUT / "code/nz_coder"
    if not code.exists():
        shutil.copytree(ROOT / "nz_coder", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for p in (code / "evaluation").iterdir():
            if p.name not in {"__init__.py", "reproducibility.py"}:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    live = OUT / (args.smoke_id if args.smoke else "live")
    live.mkdir()
    limits = RelayLimits(requests=920, input_total=15000000, output_total=2500000)
    key = None
    upstream = None
    fake = None
    if args.smoke:
        counter = lambda raw, _p: InputAccounting(len(raw), "local-fake-byte-tokens", trusted=True, exact=True,
            evidence_level="exact", contract_id="local-smoke", payload_sha256=hashlib.sha256(raw).hexdigest())
        ledger = RelayLedger(live / "ledger.jsonl", limits)
        fake = ThreadingHTTPServer(("127.0.0.1", 0), smoke_handler())
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        upstream = f"http://127.0.0.1:{fake.server_port}/v1/chat/completions"
    else:
        sys.path.insert(0, str(ARTIFACTS / "nz-provider-contract-2026-10-09/official/recipe-site"))
        from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter
        counter = DeepSeekV41Counter(ARTIFACTS / "nz-provider-contract-2026-10-09/official/audited-recipe/static/tokenizers/v41/tokenizer.json")
        grant = {"purpose": "swebench-pro-v2", "model": MODEL, "scope": [t["instance_id"] for t in manifest["tasks"]],
            "instance_manifest_sha256": hashlib.sha256((OUT / "task-manifest.json").read_bytes()).hexdigest(),
            "cost_limit": 50, "cost_currency": "CNY", "account": "project-env:DeepSeek", "input_mode": "empirical",
            "empirical_risk_accepted": True, "contract_id": counter.contract_id,
            "authorization_text": "用户本轮选择 Pro V2 公开集，回复：50元，deepseek-flash；随后明确回复：接受，按现有止损机制启动。",
            "valid_from": time.time() - 1, "valid_until": time.time() + 86400,
            "prices_cny_per_million": {"input": 2, "cached_input": 0.04, "output": 8}}
        save(live / "authorization-used.json", grant)
        from dotenv import dotenv_values
        key = os.environ.get("NZ_REVIEW_DEEPSEEK_API_KEY") or dotenv_values(ROOT / ".env").get("API_KEY")
        if not key:
            raise RuntimeError("authorized host key unavailable")
        ledger = RelayLedger(live / "ledger.jsonl", limits, empirical_authorization=grant)
    save(live / "execution-config.json", {"source_commit": command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "model": MODEL, "main_requests_per_task": 40, "auxiliary_requests_per_task": 6, "limits": asdict(limits),
        "task_seconds": 3000, "output_per_request": 8000, "stream": False, "thinking": None, "reasoning_effort": None,
        "native_entry_sha256": hashlib.sha256((DOC / "native-entry.py").read_bytes()).hexdigest(),
        "code_files_sha256": {str(p.relative_to(code)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(code.rglob("*.py"))},
        "mode": "controlled_local_smoke" if args.smoke else "autonomous_paid", "selection_manifest": hashlib.sha256((OUT / "task-manifest.json").read_bytes()).hexdigest()})
    rows = []
    archive_budget = TraceBudget(OUT / "archives", warning_bytes=858993459, hard_limit_bytes=1073741824, cleanup_target_bytes=644245094)
    try:
        for i, task in enumerate(manifest["tasks"][:1] if args.smoke else manifest["tasks"], 1):
            slot = live / f"{i:02d}"
            slot.mkdir()
            report = {"instance_id": task["instance_id"], "attempt": 0, "agent_status": None, "resolved": None}
            save(slot / "identity.json", report)
            print(f"START {i:02d} {task['instance_id']}", flush=True)
            global_stop = ("accounting_stop" if ledger.failed_contracts else
                           "estimated_fee_stop" if ledger.cost_occupied_cny + 0.064 >= 50 else
                           "request_budget_stop" if len(ledger.attempts) >= limits.requests else
                           "token_budget_stop" if ledger.input_occupied >= limits.input_total or ledger.output_occupied + 8000 > limits.output_total else
                           "storage_stop" if shutil.disk_usage(OUT).free < 1024 ** 3 else "")
            if global_stop:
                report.update(status="not_run", reason=global_stop)
                rows.append(report); save(slot / "report.json", report); save(live / "progress.json", rows)
                continue
            base = tag = case = None
            if args.smoke:
                task = {**task, "smoke_only": True}
            try:
                base, tag = runtime_image(task, slot)
                case = prepare_case(slot, tag, task, args.smoke)
                report["attempt"] = 1
                run_case(slot, case, tag, ledger, counter, key=key, upstream=upstream, smoke=args.smoke)
                patch = freeze(slot, case)
                result_path = case / "result/result.json"
                result = json.loads(result_path.read_text()) if result_path.exists() else None
                report.update(status="attempted", agent_status=result["status"] if result else "missing_result",
                              patch_sha256=hashlib.sha256(patch).hexdigest())
                if not args.smoke:
                    verdict = grade(slot, base, task, manifest, checksums)
                    report.update(resolved=verdict["resolved"], eval_status=verdict["status"])
                archive_case(slot, case, task, live.name, archive_budget, archive_instance_diagnostics)
                report["physical_requests"] = sum(a["run"] == slot.name for a in ledger.attempts.values())
            except Exception as exc:
                report.update(status="blocked" if case is None else "attempt_or_delivery_error", error=repr(exc))
            finally:
                if case is not None:
                    report["physical_requests"] = sum(a["run"] == slot.name for a in ledger.attempts.values())
                # 只回收本轮下载的镜像标签；不全局 prune，也不删除失败归档的源证据。
                if tag:
                    command(["docker", "image", "rm", tag], log=slot / "image-cleanup.txt", check=False)
                if base:
                    command(["docker", "image", "rm", base], log=slot / "base-cleanup.txt", check=False)
                    command(["docker", "image", "rm", task["configuration"]["environment"]["docker_image"]],
                            log=slot / "base-tag-cleanup.txt", check=False)
                rows.append(report); save(slot / "report.json", report); save(live / "progress.json", rows)
                print("END " + json.dumps(report, ensure_ascii=False), flush=True)
        save(live / "summary.json", {"tasks": rows, "physical_requests": len(ledger.attempts),
            "estimated_cost_occupied_cny": ledger.cost_occupied_cny, "accounting_failures": ledger.failed_contracts})
    finally:
        ledger.close()
        if fake:
            fake.shutdown(); fake.server_close()


if __name__ == "__main__":
    main()
