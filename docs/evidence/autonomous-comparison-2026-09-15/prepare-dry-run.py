"""Prepare three paired task inputs and test their initial state, without Agents."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from nz_coder.evaluation.behavioral import _fixture_b, _fixture_f
from nz_coder.evaluation.reference_adapter import InfCodeXReferenceAdapter, _workspace_hashes


ROOT = Path(__file__).resolve().parents[3]
REFERENCE = ROOT / "references/InfCodeX"
PREVIOUS = ROOT / "docs/evidence/sa-smoke-2026-09-15/environment-manifest.json"
LAUNCHER = ROOT / "tests/evaluation/fixtures/offline_exec.py"
NODE_TEST = r"""const test = require('node:test');
const assert = require('node:assert/strict');
const escape = require('./index.js');

test('ordinary literals still match', () => {
  for (const value of ['a.b', '[x]', 'a+b', 'a\\b']) {
    assert.equal(new RegExp('^' + escape(value) + '$').test(value), true);
  }
});
test('literals can be embedded in Unicode regular expressions', () => {
  for (const value of ['a-b', '[]-x', 'foo.bar']) {
    assert.equal(new RegExp('^' + escape(value) + '$', 'u').test(value), true);
  }
});
test('non-string inputs remain rejected', () => {
  for (const value of [null, 3, {}]) assert.throws(() => escape(value), TypeError);
});
"""


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    previous = json.loads(PREVIOUS.read_text())
    node = Path(previous["node"])
    assert hashlib.sha256(node.read_bytes()).hexdigest() == previous["node_sha256"]
    artifact_checks = {
        name: hashlib.sha256((REFERENCE / name).read_bytes()).hexdigest() == digest
        for name, digest in previous["files"].items()
    }
    assert all(artifact_checks.values())
    os.environ["PATH"] = f"{node.parent}:{Path(sys.executable).parent}:/usr/bin:/bin"
    capability = InfCodeXReferenceAdapter(REFERENCE).probe()  # local --version only
    assert capability.available, capability.reason
    templates = output / "initial"
    tasks = []
    for case, factory in (("F", _fixture_f), ("B", _fixture_b)):
        root = templates / case
        root.mkdir(parents=True)
        original = factory(root)
        prompt = (
            "Fix ratio for an empty count so it returns zero, preserve ordinary ratios, "
            "run the relevant tests, and report the change and verification."
            if case == "F" else
            "Rename the public product formatting API to render_product and migrate "
            "affected in-repository callers and tests. Preserve the behavior of web, CLI, "
            "mobile and export consumers. A compatibility alias may remain. Run the "
            "relevant tests and report the change and verification."
        )
        tasks.append({"id": case, "source": f"behavioral._fixture_{case.lower()}",
                      "prompt": prompt, "command": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                      "legacy_scoring_metadata_applied": False,
                      "original_capability": original.capability})
    source = REFERENCE / "node_modules/escape-string-regexp"
    root = templates / "N"
    root.mkdir()
    copied = ["index.js", "index.d.ts", "license", "package.json", "readme.md"]
    for name in copied:
        shutil.copyfile(source / name, root / name)
    assert json.loads((root / "package.json").read_text())["version"] == "2.0.0"
    (root / "literal.test.cjs").write_text(NODE_TEST)
    tasks.append({"id": "N", "source": "installed escape-string-regexp@2.0.0 (MIT), pinned reference dependency",
                  "source_hashes": {n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in copied},
                  "prompt": "Make escaped literals usable with new RegExp(..., 'u'), including hyphens. "
                            "Preserve ordinary literal matching and rejection of non-string inputs. "
                            "Run node --test literal.test.cjs and report the change and verification.",
                  "command": [str(node), "--test", "literal.test.cjs"],
                  "fixture_adaptation": "Original package copied unchanged; added public node:test contract. No npm install/test scripts."})
    runs = []
    for task in tasks:
        root = templates / task["id"]
        (root / "REQUIREMENTS.txt").write_text(task["prompt"] + "\n")
        hashes = _workspace_hashes(root)
        task["initial_hashes"] = hashes
        # Test a disposable copy; the six future Agent workspaces remain pristine.
        with tempfile.TemporaryDirectory(prefix="nz-comparison-check-") as temporary:
            temp = Path(temporary)
            work = temp / "workspace"
            shutil.copytree(root, work)
            (temp / "home").mkdir()
            env = {"HOME": str(temp / "home"), "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                   "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, str(LAUNCHER), *task["command"]]
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=30)
            task["initial_fixture_check"] = {"command": command, "exit_code": result.returncode,
                                              "stdout": result.stdout, "stderr": result.stderr,
                                              "evidence_kind": "initial_fixture_test_only"}
        for side in ("infcodex", "nzcoder"):
            work = output / "runs" / task["id"] / side / "workspace"
            shutil.copytree(root, work)
            (work.parent / "home").mkdir()
            assert _workspace_hashes(work) == hashes
            runs.append({"task": task["id"], "side": side, "repetition": 1,
                         "workspace": str(work), "session": f"autonomous-{task['id']}-{side}",
                         "status": "not_run", "reason": "missing_model_authorization",
                         "initial_hashes": hashes, "model_calls": None, "tokens": None,
                         "cost": None, "wall_time_ms": None, "final_acceptance": None,
                         "agent_verification": None, "recovery_observed": None,
                         "diff": None, "trajectory": None})
            runs[-1]["run_template"] = ({
                "prefix": list(capability.command),
                "arguments": ["--mode", "json", task["prompt"], "--agent-mode", "sa",
                              "--session", runs[-1]["session"]],
                "unresolved_arguments": {"--provider": None, "--model": None,
                                         "--effort": None, "--max-iter": None},
                "session_preparation": "official sessions.create/updateSettings in isolated HOME; no previous history",
                "session_settings": {"permissionMode": "auto-in-project", "autoModeEngine": "rules", "agentMode": "sa"},
            } if side == "infcodex" else {
                "factory": "nz_coder.runtime.execution.native_sdk.build_product_run_environment",
                "runner": "NativeSDKRunner.run_result",
                "instructions_factory": "nz_coder.runtime.conversation.prompt.build",
                "request": {"profile": "MAIN_PROFILE", "messages": [{"role": "user", "content": task["prompt"]}],
                            "stream": True, "provider": None, "model": None, "reasoning_effort": None,
                            "metadata": {"permission_mode": "auto", "persist_session": False}},
                "retrieval": "no override; policy", "tool_allowlist": "no historical experiment override",
            })
            runs[-1]["execution_ready"] = False
    manifest = {
        "evidence_kind": "authorization-gated dry run, no Agent executed",
        "nz_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "reference_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REFERENCE, text=True).strip(),
        "reference_probe": asdict(capability), "previous_build_hashes_match": artifact_checks,
        "node": str(node), "node_sha256": previous["node_sha256"],
        "authorization": {"status": "missing_model_authorization", "provider": None,
                          "endpoint": None, "model_version": None, "reasoning_effort": None,
                          "total_budget": None, "max_total_requests": None, "max_total_tokens": None},
        "planned_agent_runs": 6, "executed_agent_runs": 0, "outbound_model_requests": 0,
        "repetitions": 1, "automatic_reruns": False,
        "entrypoints": {"infcodex": "SA CLI, preconfigured-session via official sessions API",
                        "nzcoder": "build_product_run_environment -> NativeSDKRunner, unset retrieval override -> policy"},
        "not_ready": ["authorized forwarding transport and aggregate cost bounds not validated",
                      "smoke seccomp is network-only; filesystem isolation from prior evidence and other workspace not validated",
                      "actual provider request effort and streaming comparability cannot be verified without authorized selection"],
        "tasks": tasks, "runs": runs,
        "core_conclusion": "尚不能提出新的 Core 修改",
    }
    (output / "run-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"manifest": str(output / "run-manifest.json"),
                      "status": "not_run: missing_model_authorization",
                      "fixture_exit_codes": {t["id"]: t["initial_fixture_check"]["exit_code"] for t in tasks},
                      "agent_runs": 0, "outbound_model_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
