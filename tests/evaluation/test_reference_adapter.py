from __future__ import annotations

import json
import pytest

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


def test_reference_token_totals_require_explicit_cumulative_provenance():
    from nz_coder.evaluation.reference_adapter import _token_totals

    events = (
        {"type": "iteration.end", "usage": {"input_tokens": 10, "output_tokens": 2}},
        {"type": "iteration.end", "usage": {"input_tokens": 12, "output_tokens": 3}},
        {"type": "run.result", "usage_semantics": "cumulative", "usage": {"input_tokens": 22, "output_tokens": 5}},
    )
    assert _token_totals(events) == {"input": 22, "output": 5}


def test_infcodex_probe_never_falls_back_to_npx_download(tmp_path, monkeypatch):
    from nz_coder.evaluation.reference_adapter import InfCodeXReferenceAdapter

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else "/usr/bin/npx")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": "v18.19.1\n"})(),
    )
    result = InfCodeXReferenceAdapter(tmp_path).probe()
    assert result.available is False
    assert "automatic npx runtime downloads are disabled" in str(result.reason)


def test_real_emitter_missing_status_remains_unknown_and_cannot_recover():
    from pathlib import Path
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver, _json_events
    from nz_coder.evaluation.behavioral import _verification_reliability_metrics

    raw = _json_events((Path(__file__).parent / 'fixtures/infcodex-emitter.jsonl').read_text())
    assert 'status' not in raw[1] and 'exit_code' not in raw[1]
    normalized = ReferenceBehaviorDriver._normalize(raw)
    call = normalized[0]
    assert call['status'] == 'unknown'
    assert call['command_failed'] is None
    assert call['executed'] is None
    assert call['status_reason'] == 'missing_execution_status'
    failed = {**call, 'status': 'nonzero', 'command_failed': True}
    metrics = _verification_reliability_metrics((failed, call))
    assert metrics['verification_recoveries'] == 0
    assert not any(event['event'] == 'llm_response' for event in normalized)


def test_reference_unmatched_start_and_invalid_exit_remain_unknown():
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver
    events = ReferenceBehaviorDriver._normalize((
        {'type': 'tool.start', 'id': 'unfinished', 'name': 'bash', 'input': {}},
        {'type': 'tool.result', 'id': 'bad', 'name': 'bash', 'exit_code': 'not-a-code'},
    ))
    assert all(e['status'] == 'unknown' for e in events)
    assert any(e['status_reason'] == 'missing_tool_result' for e in events)


def test_infcodex_zero_exit_without_events_is_not_completed(tmp_path):
    import sys
    from nz_coder.evaluation.reference_adapter import ReferenceCapability, ReferenceRunRequest, _execute
    result = _execute('InfCodeX', ReferenceCapability('InfCodeX', True, None),
                      [sys.executable, '-c', 'pass'],
                      ReferenceRunRequest(tmp_path, 'fixture', 'local', 'local'))
    assert result.status == 'incomplete_protocol'


def test_reference_usage_snapshots_are_not_summed_as_requests():
    from nz_coder.evaluation.reference_adapter import _token_totals
    usage = {"inputTokens": 10, "outputTokens": 2}
    assert _token_totals(({"type": "iteration.end", "usage": usage},
                          {"type": "turn.completed", "usage": usage},
                          {"type": "run.result", "usage": usage})) is None


@pytest.mark.parametrize("facts,status", [
    ({"status": "success", "executed": False}, "not_executed"),
    ({"status": "cancelled"}, "cancelled"),
    ({"status": "cancelled", "executed": False}, "cancelled"),
    ({"status": "timeout"}, "timeout"),
    ({"status": "denied"}, "denied"),
    ({"exit_code": True}, "unknown"),
    ({"exit_code": 0.5}, "unknown"),
    ({"status": "banana"}, "unknown"),
])
def test_reference_non_success_states_do_not_count_as_recovery(facts, status):
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver
    from nz_coder.evaluation.behavioral import _verification_reliability_metrics
    events = ReferenceBehaviorDriver._normalize((
        {"type": "tool.start", "id": "test", "name": "bash", "input": {"command": "python -m pytest -q"}},
        {"type": "tool.result", "id": "test", **facts},
    ))
    assert events[0]["status"] == status
    failure = {**events[0], "status": "nonzero", "command_failed": True}
    assert _verification_reliability_metrics((failure, *events))["verification_recoveries"] == 0


@pytest.mark.parametrize("terminal,exit_code", [(False, 0), (True, 1)])
def test_real_process_failure_cannot_be_overridden_by_terminal(tmp_path, terminal, exit_code):
    import sys
    from nz_coder.evaluation.reference_adapter import ReferenceCapability, ReferenceRunRequest, _execute
    script = "import json; print(json.dumps(" + repr({"type": "run.result", "success": terminal}) + f")); raise SystemExit({exit_code})"
    result = _execute("InfCodeX", ReferenceCapability("InfCodeX", True, None),
                      [sys.executable, "-c", script], ReferenceRunRequest(tmp_path, "task", "local", "local"))
    assert result.status == "error"
    assert result.exit_code == exit_code
    assert json.loads(result.raw_stdout)["success"] is terminal


def test_independent_pass_does_not_prove_unknown_historical_recovery(tmp_path):
    from nz_coder.evaluation.behavioral import BehaviorBenchmarkConfig, BehaviorObservation, _fixture_f, _score
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver, _workspace_hashes
    task = _fixture_f(tmp_path)
    before = _workspace_hashes(tmp_path)
    (tmp_path / "calc/service.py").write_text("def ratio(total, count):\n    return 0 if count == 0 else total / count\n")
    observation = BehaviorObservation(final_response="done", events=ReferenceBehaviorDriver._normalize((
        {"type": "tool.start", "id": "t", "name": "bash", "input": {"command": "python -m pytest -q"}},
        {"type": "tool.result", "id": "t", "content": "1 passed"},
    )), run_result={"reference": "InfCodeX", "reference_model_calls_observable": False,
                    "metadata": {"raw_status": "completed"}})
    score = _score(task, tmp_path, before, observation, 1, BehaviorBenchmarkConfig(model="local"))
    assert score["verification"]["passed"] is True
    assert score["recovery_complete"] is None
    assert score["success"] is False
    assert score["metrics"]["model_calls"] is None


def test_recorded_real_cli_tools_remain_unknown():
    from pathlib import Path
    from nz_coder.evaluation.reference_adapter import ReferenceBehaviorDriver, _json_events, _token_totals
    root = Path(__file__).parent / "fixtures"
    raw = _json_events((root / "infcodex-cli-real.jsonl").read_text())
    assert raw[-1]["type"] == "run.result" and raw[-1]["success"] is True
    tools = [e for e in ReferenceBehaviorDriver._normalize(raw) if e["event"] == "tool_call"]
    assert [e["name"] for e in tools] == ["read", "bash", "edit", "bash"]
    assert all(e["status"] == "unknown" and e["executed"] is None for e in tools)
    assert "ZeroDivisionError" in tools[1]["output"] and "1 passed" in tools[3]["output"]
    assert _token_totals(raw) is None
    incomplete = _json_events((root / "infcodex-cli-incomplete.jsonl").read_text())
    assert not any(e["type"] == "run.result" for e in incomplete)
    unfinished = [e for e in ReferenceBehaviorDriver._normalize(incomplete) if e["event"] == "tool_call"]
    assert unfinished[0]["status_reason"] == "missing_tool_result"


def test_stored_unknown_recovery_cannot_be_rescored_as_success(tmp_path):
    from nz_coder.evaluation.reference_adapter import rescore_reference_matrix
    target = tmp_path / "stored.json"
    target.write_text(json.dumps({"runs": [{
        "task": {"capability": "verification-recovery"},
        "score": {"recovery_complete": None, "verification": {"passed": True},
                  "final_patch_correctness": True, "run_result": {"reference_model_calls_observable": False}},
    }]}))
    result = rescore_reference_matrix(target)
    assert result["runs"][0]["score"]["success"] is False
    assert result["runs"][0]["score"]["long_horizon_exercised"] is None


@pytest.mark.parametrize("terminal", [{"type": "run.result"}, {"type": "run.result", "success": "true"}])
def test_invalid_terminal_success_is_unknown_protocol(tmp_path, terminal):
    import sys
    from nz_coder.evaluation.reference_adapter import ReferenceCapability, ReferenceRunRequest, _execute
    result = _execute("InfCodeX", ReferenceCapability("InfCodeX", True, None),
        [sys.executable, "-c", "import json; print(json.dumps(" + repr(terminal) + "))"],
        ReferenceRunRequest(tmp_path, "task", "local", "local"))
    assert result.status == "incomplete_protocol"
    assert "no valid boolean" in result.error
