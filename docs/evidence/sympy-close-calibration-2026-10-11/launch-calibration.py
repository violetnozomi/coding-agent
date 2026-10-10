"""本例的一次性启动命令；复用已有隔离入口、预算转发和官方评分 CLI。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
INSTANCE = "sympy__sympy-22914"
IMAGE = "nz-swe-sympy-bound:20261011"
ARTIFACTS = Path.home() / ".codex/artifacts"
sys.path.insert(0, str(ARTIFACTS / "nz-provider-contract-2026-10-09/official/recipe-site"))
BASE = ARTIFACTS / "nz-swebench-verified-2026-10-10/preparation" / INSTANCE
TOKENIZER = ARTIFACTS / "nz-provider-contract-2026-10-09/official/audited-recipe/static/tokenizers/v41/tokenizer.json"
EVALUATOR = ARTIFACTS / "nz-swebench-verified-2026-10-10/harness-self-test/evaluator-only.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter
    from nz_coder.evaluation.model_relay import RelayBinding, RelayLedger, RelayLimits, RelayServer, empirical_authorization_valid

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output-root", type=Path, default=ARTIFACTS / "nz-sympy-autonomous-2026-10-11")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    output = args.output_root.resolve()
    run_id = "nz-sympy-autonomous-20261011"
    counter = DeepSeekV41Counter(TOKENIZER)
    limits = RelayLimits(requests=30, input_total=500000, output_total=100000)
    packet = json.loads((ROOT / "docs/evidence/swebench-official-protocol-2026-10-10/inputs/calibration.json").read_text())
    packet["instances"] = [row for row in packet["instances"] if row["instance_id"] == INSTANCE]
    row = packet["instances"][0]
    assert set(row) == {"instance_id", "repo", "base_commit", "problem_statement"}
    public = (json.dumps(packet, ensure_ascii=False, indent=2) + "\n").encode()
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requested = dict(purpose="swebench-verified", scope=[INSTANCE], account="project-env:DeepSeek",
                     model="deepseek-v4-flash", input_mode="empirical", empirical_risk_accepted=True,
                     instance_manifest_sha256=hashlib.sha256(public).hexdigest(), contract_id=counter.contract_id,
                     source_commit=source, startup_source_sha256=digest(Path(__file__)),
                     relay_source_sha256=digest(ROOT / "nz_coder/evaluation/model_relay.py"),
                     bounds=vars(limits), main_request_cap=24, auxiliary_request_cap=6,
                     output_limit=8000, agent_seconds=900, thinking=None, reasoning_effort=None, stream=False,
                     single_attempt=True, run_id=run_id, output_root=str(output))
    grant = json.loads(args.authorization.read_text()) if args.authorization else None
    allowed = (empirical_authorization_valid(grant) and all(grant.get(k) == v for k, v in requested.items()))
    admission = dict(online_allowed=bool(allowed), online_status="authorized" if allowed else "not_run: missing_paid_authorization",
                     online_requests=0, requested=requested,
                     missing_fields=["authorization_text", "cost_limit", "cost_currency=CNY", "valid_from", "valid_until",
                                     "prices_cny_per_million"] if not allowed else [])
    print(json.dumps(admission, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if not allowed:
        return 2

    assert not subprocess.check_output(["git", "status", "--porcelain", "--", "nz_coder",
        "scripts/swebench_relay_entry.py", "tests/evaluation/fixtures/swebench_close_observer.py"], cwd=ROOT)

    # 授权成立后才读取宿主已有账号；真实密钥不进入容器、命令行或证据文件。
    from dotenv import dotenv_values
    key = os.environ.get("NZ_REVIEW_DEEPSEEK_API_KEY") or dotenv_values(ROOT / ".env").get("API_KEY")
    if not key:
        raise RuntimeError("authorized account key unavailable on host")
    assert version("swebench") == "4.1.0"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BASE, text=True).strip() == row["base_commit"]
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=BASE)
    inspect = subprocess.check_output(["docker", "image", "inspect", IMAGE], text=True)
    output.mkdir(parents=True, exist_ok=False)
    (output / "authorization-used.json").write_text(json.dumps(grant, ensure_ascii=False, indent=2))
    (output / "admission.json").write_text(json.dumps(admission, ensure_ascii=False, indent=2))
    code = output / "code-new/nz_coder"
    shutil.copytree(ROOT / "nz_coder", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for child in (code / "evaluation").iterdir():
        if child.name not in {"__init__.py", "reproducibility.py"}:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    case = output / "sympy-calibration"
    for name in ("workspace", "home", "tmp", "input", "sockets", "result", "control", "driver"):
        (case / name).mkdir(parents=True)
    bare = case / "workspace/.nz-coder/swebench-lite/repo-cache/sympy_sympy.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--bare", str(BASE), str(bare)], check=True, capture_output=True)
    (case / "input/instances.json").write_bytes(public)
    entry = (ROOT / "scripts/swebench_relay_entry.py").read_text()
    entry = entry.replace('if __name__ == "__main__":', '''from nz_coder.runtime.execution import composition
from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS, strict_bash_violation
from nz_coder.runtime.process.workdir import current_workdir
import json
original_environment = composition.build_product_environment

def approve(name, arguments):
    workspace = current_workdir().resolve()
    allowed = (workspace.parent == Path('/workspace/runs') and name in STRICT_ALLOWED_TOOLS
               and (name != 'bash' or not strict_bash_violation(str(arguments.get('command') or ''))))
    with Path('/result/permissions.jsonl').open('a') as sink:
        sink.write(json.dumps(dict(tool=name,input=arguments,allowed=allowed,workspace=str(workspace))) + '\\n')
    return allowed

def environment(system_prompt, **kwargs):
    return original_environment(system_prompt, permission_asker=approve, **kwargs)

composition.build_product_environment = environment
from swebench_close_observer import install
install()

if __name__ == "__main__":''')
    (case / "driver/entry.py").write_text(entry)
    shutil.copy2(ROOT / "tests/evaluation/fixtures/swebench_close_observer.py", case / "driver/swebench_close_observer.py")
    spec = importlib.util.spec_from_file_location("existing_launcher", ROOT / "scripts/review_effects_execution.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    argv = launcher.launch_argv(output, case, image=IMAGE)
    argv.insert(2, "--pull=never")
    index = argv.index(IMAGE)
    argv[index:index] = ["--env", "MAX_AGENT_TURNS=24", "--env", "NZ_SWE_NOMINAL_AGENT_TURNS=24",
                         "--env", "MAX_OUTPUT_TOKENS=8000", "--env", "NZ_PROVIDER_MAX_RETRIES=0"]
    argv[argv.index(IMAGE) + 1] = "/usr/local/bin/python"
    argv += ["run-agent", "--profile", "verified", "--instances-file", "/input/instances.json",
             "--run-id", run_id, "--work-root", "/workspace/runs", "--output", "/result/predictions.jsonl",
             "--clone-timeout", "15", "--agent-timeout", "900", "--no-cleanup-worktrees",
             "--trace-budget-gib", "0.5", "--trace-warning-gib", "0.4", "--trace-cleanup-target-gib", "0.3"]
    (output / "launch.json").write_text(json.dumps(dict(argv=argv, image=json.loads(inspect)[0]["Id"],
        source_commit=source, runtime_files={str(p.relative_to(code)): digest(p) for p in code.rglob("*") if p.is_file()}), indent=2))
    ledger = RelayLedger(output / "ledger.jsonl", limits, empirical_authorization=grant)
    servers = []
    deadline = time.monotonic() + 900
    try:
        for role, cap in (("main", 24), ("auxiliary", 6)):
            server = RelayServer(case / "sockets" / (role + ".sock"), ledger=ledger,
                binding=RelayBinding("swebench-verified", INSTANCE, case.name, role, cap, "deepseek-v4-flash", 8000, deadline, role == "main"),
                upstream="https://api.deepseek.com/v1/chat/completions", counter=counter,
                remote_authorized=True, api_key=key, trace_directory=output / "private-provider")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        with (output / "stdout.txt").open("w") as stdout, (output / "stderr.txt").open("w") as stderr:
            result = subprocess.run(argv, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                                    stdout=stdout, stderr=stderr, timeout=1080)
        (output / "cli-exit.json").write_text(json.dumps(dict(returncode=result.returncode)))
    finally:
        ledger.cancel(case.name)
        subprocess.run(["docker", "rm", "-f", "nz-review-" + case.name], capture_output=True)
        for server in servers:
            server.shutdown()
            server.server_close()
        ledger.close()
    reports = json.loads((case / "result/predictions.report.json").read_text())
    if reports[0]["patch_status"] not in {"present", "empty"}:
        raise RuntimeError("patch not safely frozen; official evaluation not started")
    evaluation = output / "evaluation"
    evaluation.mkdir()
    command = [sys.executable, "-m", "nz_coder.swebench", "run-eval", "--profile", "verified",
               "--dataset-file", str(EVALUATOR), "--split", "test", "--predictions-path", str(case / "result/predictions.jsonl"),
               "--instance-ids", INSTANCE, "--run-id", run_id + "-official", "--timeout", "300", "--max-workers", "1",
               "--image-namespace", "swebench", "--image-arch", "x86_64", "--instance-image-tag", "latest", "--no-package"]
    (evaluation / "command.json").write_text(json.dumps(command, indent=2))
    with (evaluation / "stdout.txt").open("w") as stdout, (evaluation / "stderr.txt").open("w") as stderr:
        result = subprocess.run(command, cwd=evaluation, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()),
            "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT), "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"},
            stdout=stdout, stderr=stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
