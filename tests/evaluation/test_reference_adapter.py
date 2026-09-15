from __future__ import annotations

import json

def test_reference_probes_report_current_runtime_blockers(tmp_path, monkeypatch) -> None:
    from nz_coder.evaluation.reference_adapter import (
        InfCodeXReferenceAdapter, OpenCodeReferenceAdapter,
    )

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": "v18.19.1\n"})(),
    )

    infcodex = InfCodeXReferenceAdapter(tmp_path).probe()
    opencode = OpenCodeReferenceAdapter(tmp_path).probe()

    assert infcodex.available is False
    assert "node runtime >=20" in str(infcodex.reason)
    assert opencode.available is False
    assert "bun is not installed" in str(opencode.reason)


def test_unavailable_reference_run_does_not_mutate_workspace(tmp_path, monkeypatch) -> None:
    from nz_coder.evaluation.reference_adapter import (
        InfCodeXReferenceAdapter, ReferenceRunRequest,
    )

    target = tmp_path / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = InfCodeXReferenceAdapter(tmp_path / "reference").run(ReferenceRunRequest(
        workspace=tmp_path, prompt="change VALUE", model="model", provider="provider",
    ))

    assert result.status == "unavailable"
    assert result.changed_files == ()
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_reference_execution_captures_json_events_and_changed_files(tmp_path) -> None:
    from nz_coder.evaluation.reference_adapter import (
        ReferenceCapability, ReferenceRunRequest, _execute,
    )

    script = tmp_path / "agent.py"
    script.write_text(
        "from pathlib import Path\n"
        "import json\n"
        "Path('target.py').write_text('VALUE = 2\\n')\n"
        "print(json.dumps({'type': 'message', 'text': 'done'}))\n",
        encoding="utf-8",
    )
    (tmp_path / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    capability = ReferenceCapability("fixture", True, None)
    result = _execute(
        "fixture", capability, ["python", str(script)],
        ReferenceRunRequest(tmp_path, "task", "model", "provider", timeout_s=5),
    )

    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.changed_files == ("target.py",)
    assert result.trajectory[0]["type"] == "message"


def test_reference_driver_normalizes_tool_and_usage_events() -> None:
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver

    events = ReferenceBehaviorDriver._normalize((
        {"type": "iteration.start", "iter": 1},
        {"type": "tool.start", "id": "call-1", "name": "read", "input": {"path": "a.py"}},
        {"type": "tool.result", "id": "call-1", "name": "read", "content": "body"},
        {"type": "iteration.end", "usage": {"inputTokens": 12, "outputTokens": 3}},
    ))

    assert events[0]["event"] == "tool_call"
    assert events[0]["name"] == "read"
    assert events[0]["output"] == "body"
    assert events[1]["input_tokens"] == 12


def test_unobservable_reference_turn_count_does_not_fail_long_horizon(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import (
        BehaviorBenchmarkConfig, BehaviorObservation, BehaviorTask, _score,
    )

    task = BehaviorTask("E", "long-horizon", "task", (), (), min_turns=15)
    observation = BehaviorObservation(
        final_response="completed",
        run_result={
            "reference": "fixture-reference",
            "reference_trajectory_available": False,
            "metadata": {"raw_status": "completed"},
        },
    )

    score = _score(
        task, tmp_path, {}, observation, 1.0,
        BehaviorBenchmarkConfig(model="fixture"),
    )

    assert score["success"] is True
    assert score["turn_requirement_observable"] is False
    assert score["long_horizon_exercised"] is None


def test_short_correct_run_is_success_but_does_not_claim_long_horizon(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import (
        BehaviorBenchmarkConfig, BehaviorObservation, BehaviorTask, _score,
    )

    task = BehaviorTask("E", "long-horizon", "task", (), (), min_turns=15)
    observation = BehaviorObservation(
        final_response="completed",
        events=({"event": "llm_response"},) * 9,
        run_result={"metadata": {"raw_status": "completed"}},
    )

    score = _score(
        task, tmp_path, {}, observation, 1.0,
        BehaviorBenchmarkConfig(model="fixture"),
    )

    assert score["success"] is True
    assert score["turn_requirement_observable"] is True
    assert score["long_horizon_exercised"] is False


def test_reference_rescore_uses_stored_evidence_only(tmp_path, monkeypatch) -> None:
    from nz_coder.evaluation.reference_adapter import rescore_reference_matrix

    def unexpected_process(*_args, **_kwargs):
        raise AssertionError("rescoring must not execute the fixture")

    monkeypatch.setattr("subprocess.run", unexpected_process)
    monkeypatch.setattr("subprocess.Popen", unexpected_process)
    target = tmp_path / "reference.json"
    target.write_text(json.dumps({
        "runs": [{
            "task": {
                "case_id": "E", "capability": "long-horizon", "min_turns": 15,
                "expected_files": [], "expected_symbols": [], "expected_call_path": [],
            },
            "score": {
                "success": False, "error": "", "final_patch_correctness": True,
                "verification": {"passed": True}, "recovery_complete": True,
                "child_execution_complete": True, "no_unneeded_web": True,
                "correct_files": [], "correct_symbols": [], "metrics": {"turns": 0},
                "run_result": {
                    "reference": "fixture", "reference_trajectory_available": False,
                },
            },
        }],
        "success_rate": 0.0,
    }), encoding="utf-8")

    result = rescore_reference_matrix(target)

    assert result["success_rate"] == 1.0
    assert result["runs"][0]["score"]["long_horizon_exercised"] is None
    assert result["scorer_revision"] == "completion-correctness-v2"


def test_infcodex_reference_uses_json_mode_sa_and_preserves_effort(tmp_path, monkeypatch):
    from nz_coder.evaluation.reference_adapter import (
        InfCodeXReferenceAdapter, ReferenceCapability, ReferenceRunRequest,
    )

    adapter = InfCodeXReferenceAdapter(tmp_path / "reference")
    capability = ReferenceCapability("InfCodeX", True, None, ("node", "cli.js"))
    monkeypatch.setattr(adapter, "probe", lambda: capability)
    captured = {}

    def fake_execute(name, cap, command, request, *, env_overrides=None):
        captured.update(command=command, request=request, env=env_overrides)
        return type("Result", (), {"status": "completed"})()

    monkeypatch.setattr("nz_coder.evaluation.reference_adapter._execute", fake_execute)
    adapter.run(ReferenceRunRequest(
        tmp_path, "fix auth", "model", "provider", reasoning="medium",
    ))
    command = captured["command"]
    assert "--print" not in command
    assert command[2:4] == ["--mode", "json"]
    assert "fix auth" in command
    assert command[command.index("--agent-mode") + 1] == "sa"
    assert command[command.index("--effort") + 1] == "medium"


def test_reference_behavior_matrix_honors_one_repetition(tmp_path, monkeypatch):
    from nz_coder.evaluation.reference_adapter import (
        ReferenceCapability, run_reference_behavior_matrix,
    )

    class Adapter:
        name = "fixture-reference"
        def probe(self):
            return ReferenceCapability(self.name, True, None)

    calls = []
    class Benchmark:
        def __init__(self, *_args):
            pass
        def run_case(self, case_id, config):
            calls.append((case_id, config.repetition))
            return {"score": {"success": True}}

    monkeypatch.setattr("nz_coder.evaluation.behavioral.AgentBehaviorBenchmark", Benchmark)
    result = run_reference_behavior_matrix(
        tmp_path, adapter=Adapter(), provider="fixture", model="model",
        repetitions=1, case_ids=("A",),
    )
    assert calls == [("A", 1)]
    assert result["repetitions"] == 1


def test_reference_event_normalization_uses_structured_failure_facts():
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver

    events = ReferenceBehaviorDriver._normalize((
        {"type": "tool.started", "id": "c1", "name": "bash", "input": {}},
        {"type": "tool.completed", "id": "c1", "content": "previous run failed; retrying", "status": "success"},
        {"type": "tool.started", "id": "c2", "name": "bash", "input": {}},
        {"type": "tool.completed", "id": "c2", "content": "diagnostic", "exit_code": 1},
    ))
    assert events[0]["status"] == "ok"
    assert events[0]["command_failed"] is False
    assert events[1]["status"] == "nonzero"
    assert events[1]["command_failed"] is True


def test_reference_token_totals_prefer_cumulative_terminal_usage():
    from nz_coder.evaluation.reference_adapter import _token_totals

    events = (
        {"type": "iteration.end", "usage": {"input_tokens": 10, "output_tokens": 2}},
        {"type": "iteration.end", "usage": {"input_tokens": 12, "output_tokens": 3}},
        {"type": "run.result", "usage": {"input_tokens": 22, "output_tokens": 5}},
    )
    assert _token_totals(events) == {"input": 22, "output": 5, "reasoning": 0, "cache": 0}
