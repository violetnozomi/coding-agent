"""复用真实 Runner 回放，只捕获生产审查边界；不返回语义裁决。"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest


class PreparationComplete(BaseException):
    """首个审查边界结束离线准备，不进入第二次决策。"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code))
    from tests.runtime.test_requirement_scope_runtime import _run
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.verification import sidecar_verifier as sidecar
    from nz_coder.tools.files import _safe_path

    spec = json.loads(args.input.read_text())
    capture = {"preparation_only": True, "paid_requests": 0,
               "review_response": None, "usage": None,
               "main_protocol_inputs": [], "review_boundary_inputs": []}
    holder = {}
    original_build = native_sdk.build_product_run_environment
    original_evidence = sidecar.SidecarVerifierHook._evidence
    original_stop = sidecar.SidecarVerifierHook.__call__

    def build(request, options, **kwargs):
        env = original_build(request, options, **kwargs)
        holder["env"] = env
        env.repo_intelligence.prewarm().result(timeout=15)
        runtime = native_sdk.resolve_model_runtime(None)
        original_complete = runtime.provider.create_completion

        def complete(client, **fields):
            names = [t.get("function", {}).get("name") for t in fields.get("tools", [])]
            if names == ["emit_sidecar_verdict"]:
                capture["review_boundary_inputs"].append(copy.deepcopy(fields))
                # 不给模型回包；生产回退 trace 仅是离线拒绝发送的后果。
                raise RuntimeError("offline preparation: model transport is disabled")
            capture["main_protocol_inputs"].append(copy.deepcopy(fields))
            return original_complete(client, **fields)

        runtime.provider.create_completion = complete
        capture["entry"] = {"environment": type(env).__name__,
                            "runner": type(env.runner).__name__,
                            "retrieval": env.repo_retrieval_strategy}
        return env

    def evidence(hook, context):
        packet, metrics, risk = original_evidence(hook, context)
        capture["packet"] = dataclasses.asdict(packet)
        capture["metrics"] = dataclasses.asdict(metrics)
        capture["compatibility_hypothesis"] = risk
        capture["review_user_message"] = sidecar.build_verifier_user_message(packet)
        capture["stop_context"] = dataclasses.asdict(context)
        capture["state_before_review"] = copy.deepcopy(hook._loop.runtime_state.to_dict())
        return packet, metrics, risk

    async def stop(hook, context):
        decision = await original_stop(hook, context)
        capture["offline_gate_observation"] = {
            "decision": dataclasses.asdict(decision), "stats": copy.deepcopy(hook.stats),
            "source": ("pre_model_rule" if hook.stats["last_trace"] == "deterministic_compatibility_guard"
                       else "offline_transport_refusal"),
            "semantic_verdict_valid": False,
        }
        capture["state_after_review"] = copy.deepcopy(hook._loop.runtime_state.to_dict())
        raise PreparationComplete()

    actions = [
        [("read_file", {"path": "payment.py"}),
         ("read_file", {"path": "tests/test_amount.py"})],
        [("edit_file", {"path": "payment.py", "old_text": spec["old_text"],
                        "new_text": spec["new_text"]})],
        [("bash", {"command": "python -m pytest -q tests"})],
        spec["neutral_report"],
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(native_sdk, "build_product_run_environment", build)
        patch.setattr(sidecar.SidecarVerifierHook, "_evidence", evidence)
        patch.setattr(sidecar.SidecarVerifierHook, "__call__", stop)
        try:
            _run(patch, args.workspace, spec["task"], actions, label="preparation")
        except PreparationComplete:
            pass
        finally:
            env = holder.get("env")
            if env is not None:
                capture["runtime"] = [json.loads(line) for line in env.tracer.path.read_text().splitlines()]
                capture["state"] = env.runtime_state.to_dict()
                capture["verification_manager"] = env.vm.status()
                capture["diff"] = env.change_tracker.render_current_diff()
                database = args.workspace / ".nz-coder/index/code-index.sqlite3"
                with sqlite3.connect(database) as connection:
                    capture["index_schema"] = connection.execute("PRAGMA user_version").fetchone()[0]

    # 仅核对既有文件工具的工作区边界；这不是宿主文件系统隔离证明。
    from nz_coder.state.workdir import scoped_workdir
    with scoped_workdir(args.workspace):
        denials = []
        for target in (str(args.input), str(args.code / "docs/evidence"), "../input.json"):
            try:
                _safe_path(target)
            except Exception as exc:
                denials.append({"target": target, "denied": True, "exception": type(exc).__name__})
            else:
                denials.append({"target": target, "denied": False})
        capture["file_tool_boundary"] = denials
    assert capture.get("packet"), "production stop hook not reached"
    assert len(capture["review_boundary_inputs"]) <= 1, "unexpected preparation review retry"
    assert all(item["denied"] for item in denials)
    capture["workspace_hashes"] = {name: hashlib.sha256((args.workspace / name).read_bytes()).hexdigest()
                                   for name in ("payment.py", "tests/test_amount.py")}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "capture.json").write_text(json.dumps(capture, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"packet_generated": True, "schema": capture["index_schema"],
                      "source": capture["offline_gate_observation"]["source"],
                      "held_review_requests": len(capture["review_boundary_inputs"]), "paid_requests": 0}))


if __name__ == "__main__":
    main()
