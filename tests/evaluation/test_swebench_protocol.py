"""SWE 适配层的真实 Git 产物和单次尝试契约。"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.swebench.artifacts import AttemptJournal
from nz_coder.swebench.orchestrator import RetryOrchestrator, _collect_diff, _write_prediction


def offline_environment(system_prompt, *, actions=None, **kwargs):
    """仅替换模型边界，spawn 内仍构造完整生产环境。"""
    import socket
    from nz_coder.foundation import config
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.execution import loop, native_sdk
    from nz_coder.runtime.execution.composition import build_product_environment
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.runtime.process.workdir import current_workdir

    workspace = current_workdir()
    plan = actions if actions is not None else json.loads((workspace / ".nz-coder-runs" / "offline-actions.json").read_text())
    os.environ["HOME"] = str(workspace.parent / "isolated-worker-home")
    config.API_KEY = "local-only"
    config.PROVIDER_MAX_RETRIES = 0
    capabilities = ModelCapabilities(provider="offline", model_id="offline-model", supports_streaming=False)

    def deny_network(*_args, **_kwargs):
        raise AssertionError("offline SWE test attempted network access")

    socket.socket.connect = deny_network

    class Provider:
        name = "offline"
        position = 0

        def capabilities(self, _model):
            return capabilities

        def create_client(self):
            return SimpleNamespace(close=lambda: None)

        def create_completion(self, _client, **request):
            names = [t.get("function", {}).get("name") for t in request.get("tools", [])]
            role = "auxiliary" if names == ["emit_sidecar_verdict"] else "main"
            with (workspace / ".nz-coder-runs" / "actual-provider-requests.jsonl").open("a") as output:
                output.write(json.dumps({"role": role, "request": request}) + "\n")
            if role == "auxiliary":
                actions = [["emit_sidecar_verdict", {"verdict": "accept", "reason": "Offline verdict only"}]]
            else:
                actions = plan[self.position]
                self.position += 1
                if actions == "RAISE_PROVIDER":
                    raise RuntimeError("offline injected Provider boundary failure")
                if actions == "READ_PREVIOUS_ARTIFACT":
                    import re
                    ids = re.findall(r"artifact_[a-f0-9]{32}", json.dumps(request["messages"]))
                    assert ids, "real failed tool result must expose its controlled artifact"
                    actions = [["read_tool_result", {"artifact_id": ids[-1], "max_bytes": 65536}]]
            calls = [] if isinstance(actions, str) else [SimpleNamespace(
                id=f"call-{self.position}-{i}", type="function",
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                for i, (name, arguments) in enumerate(actions)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=actions if isinstance(actions, str) else "", tool_calls=calls),
                finish_reason="tool_calls" if calls else "stop")],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6))

    provider = Provider()
    runtime = ResolvedModelRuntime(provider_id="offline", model_id="offline-model", request_model_id="offline-model",
        variant=None, provider=provider, client=provider.create_client(), capabilities=capabilities, owns_client=True)
    import nz_coder.runtime.model_gateway as gateway
    for target in (loop, native_sdk, gateway):
        target.resolve_model_runtime = lambda *_a, **_k: runtime
    return build_product_environment(system_prompt, **kwargs, model_runtime=runtime, manage_model_runtime=False,
        permission_asker=lambda name, arguments: name == "bash" and not __import__(
            "nz_coder.swebench.policy", fromlist=["strict_bash_violation"]
        ).strict_bash_violation(arguments.get("command", "")))


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "offline@example.invalid")
    git(repo, "config", "user.name", "Offline regression")
    for name in ("source.py", "deleted.py", "old.py"):
        (repo / name).write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def test_collect_diff_keeps_index_and_all_real_files(repository, tmp_path):
    base = tmp_path / "independent-base"
    subprocess.run(["git", "clone", "-q", str(repository), str(base)], check=True)
    (repository / "source.py").write_text("VALUE = 2\n")
    git(repository, "add", "source.py")
    (repository / "source.py").write_text("VALUE = 3\n")
    (repository / "deleted.py").unlink()
    git(repository, "mv", "old.py", "renamed.py")
    (repository / "test_new_feature.py").write_text("assert True\n")
    (repository / ".nz-coder-runs").mkdir()
    (repository / ".nz-coder-runs" / "private.txt").write_text("INTERNAL")
    index_before = (repository / ".git" / "index").read_bytes()
    patch = _collect_diff(repository)
    assert (repository / "test_new_feature.py").read_text() == "assert True\n"
    assert (repository / ".git" / "index").read_bytes() == index_before
    assert "INTERNAL" not in patch
    subprocess.run(["git", "apply", "-"], cwd=base, input=patch, text=True, check=True)
    assert (base / "source.py").read_text() == "VALUE = 3\n"
    assert (base / "test_new_feature.py").is_file()
    assert (base / "renamed.py").is_file()
    assert not (base / "old.py").exists()
    assert not (base / "deleted.py").exists()


@pytest.mark.parametrize("status", ["completed", "max_turns", "timeout", "error"])
def test_prediction_paths_preserve_identical_real_patch(repository, tmp_path, status):
    (repository / "source.py").write_text("VALUE = 2\n")
    patch = _collect_diff(repository)
    result = {"status": "completed" if status == "completed" else "agent_failed",
              "agent_status": {"status": status}, "model_patch": patch}
    output = io.StringIO()
    _write_prediction(output, "owner__repo-1", "offline", result)
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    journal.record({"instance_id": "owner__repo-1", "attempt": 1,
                    "prediction": json.loads(output.getvalue())})
    journal.write_predictions(tmp_path / "predictions.jsonl")
    assert json.loads(output.getvalue())["model_patch"] == patch
    assert json.loads((tmp_path / "predictions.jsonl").read_text())["model_patch"] == patch


def test_open_claim_preserves_interrupted_workspace_and_trace(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    work = root / "owner__repo-1"
    work.mkdir(parents=True)
    (work / "source.py").write_text("FIRST ATTEMPT CHANGE\n")
    (work / "request.jsonl").write_text('{"call_id":"first-actual-request"}\n')
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    journal.claim("owner__repo-1")
    runner = RetryOrchestrator(None, None)

    def forbidden_restart(*_args, **_kwargs):
        pytest.fail("open claim must not create a new Session or restart inference")

    monkeypatch.setattr(runner, "run_instance", forbidden_restart)
    result = runner.run_batch([{"instance_id": "owner__repo-1"}], work_root=root,
        run_id="interrupted", config=None, build_prompt=None, agent_cls=None,
        trace_cls=None, clone_timeout=1, agent_timeout=1, empty_patch_retries=0,
        pred_file=None, model_name="offline", attempt_journal=journal,
        cleanup_worktrees=True)
    assert result[0]["agent_status"]["status"] == "interrupted"
    assert (work / "source.py").read_text() == "FIRST ATTEMPT CHANGE\n"
    assert (work / "request.jsonl").is_file()
    assert journal.completed_ids() == set()


def test_archive_relocates_owned_session_artifact(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import save_session
    from nz_coder.swebench.trace_budget import TraceBudget, archive_instance_diagnostics
    from nz_coder.tool_platform.artifacts import ArtifactStore

    root = tmp_path / "runs"
    work = root / "owner__repo-1"
    work.mkdir(parents=True)
    storage = tmp_path / "original-private-storage"
    monkeypatch.setattr(config, "SESSION_DIR", storage)
    with scoped_workdir(work):
        store = ArtifactStore(work, "owned-session")
        aid = store.put("UNIQUE MIDDLE FAILURE at source.py:17", kind="tool-result")
        save_session([{"role": "tool", "content": aid}], session_id="owned-session", require_aliases=False)
        ArtifactStore(work, "other-session").put("OTHER PRIVATE INSTANCE", kind="tool-result")
    trace = work / "trace.jsonl"
    trace.write_text(json.dumps({"artifact_id": aid}) + "\n")
    result = archive_instance_diagnostics(instance_id="owner__repo-1", workdir=work,
        run_root=root, trace_path=trace, public_input_path=None,
        metadata={"session_id": "owned-session", "model_patch": ""},
        budget=TraceBudget(tmp_path / "archive", 50000, 100000, 25000))
    relocated = tmp_path / "independent-archive"
    shutil.move(result.bundle_path, relocated)
    shutil.rmtree(storage)
    monkeypatch.setattr(config, "SESSION_DIR", relocated / "sessions")
    recovered = ArtifactStore(work, "owned-session")
    assert recovered.read(aid) == "UNIQUE MIDDLE FAILURE at source.py:17"
    assert "OTHER PRIVATE INSTANCE" not in "".join(p.read_text() for p in relocated.rglob("*.txt"))
    index = json.loads((relocated / "evidence-index.json").read_text())
    assert index["artifact_ids"][aid]["sha256"]
    assert index["http_capture"] == "unavailable"


def test_archive_quota_preserves_only_source(tmp_path):
    from nz_coder.swebench.trace_budget import TraceBudget, archive_instance_diagnostics

    work = tmp_path / "runs" / "owner__repo-1"
    work.mkdir(parents=True)
    trace = work / "trace.jsonl"
    trace.write_text('{"large":"' + "X" * 1000 + '"}\n')
    with pytest.raises(OSError, match="quota"):
        archive_instance_diagnostics(instance_id=work.name, workdir=work, run_root=work.parent,
            trace_path=trace, public_input_path=None, metadata={},
            budget=TraceBudget(tmp_path / "archive", 100, 200, 50))
    assert trace.is_file()
    assert not (tmp_path / "archive" / work.name).exists()


def test_harness_effective_config_and_changed_patch_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nz_coder.swebench.adapter import SWEBenchAdapter

    monkeypatch.chdir(tmp_path)
    adapter = SWEBenchAdapter("verified")
    monkeypatch.setattr(adapter, "_check_module", lambda _: (True, "", ""))
    monkeypatch.setattr(adapter, "_check_docker", lambda: (True, "", ""))
    commands = []
    monkeypatch.setattr("nz_coder.swebench.adapter.subprocess.run", lambda cmd:
        commands.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"instance_id": "owner__repo-1",
        "model_name_or_path": "offline", "model_patch": "first patch"}) + "\n")
    args = SimpleNamespace(profile="verified", split="dev", dataset_file=None,
        instance_ids=["owner__repo-1"], prepull_timeout=0, max_workers=1,
        run_id="fixed", timeout=60, clean=False, image_namespace="local-custom",
        image_arch="x86_64", instance_image_tag="pinned")
    assert adapter.run_harness(predictions, args) == 0
    command = commands[-1]
    assert command[command.index("--split") + 1] == "dev"
    assert command[command.index("--namespace") + 1] == "local-custom"
    assert command[command.index("--instance_image_tag") + 1] == "pinned"
    predictions.write_text(predictions.read_text().replace("first patch", "second patch"))
    assert adapter.run_harness(predictions, args) == 2
    assert len(commands) == 1


def test_invalid_official_resolved_is_unknown(tmp_path):
    from nz_coder.swebench.adapter import SWEBenchAdapter

    logs = tmp_path / "owner__repo-1"
    logs.mkdir()
    (logs / "report.json").write_text('{"owner__repo-1":{"resolved":"false"}}')
    assert SWEBenchAdapter().load_feedback(logs.name, tmp_path).resolved == "unknown"


def test_official_summary_keeps_missing_and_filtered_instances(tmp_path, monkeypatch):
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.submission import _report_resolved

    monkeypatch.chdir(tmp_path)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("".join(json.dumps({"instance_id": f"owner__repo-{i}",
        "model_name_or_path": "offline", "model_patch": "patch" if i != 4 else ""}) + "\n"
        for i in range(1, 5)))
    root = tmp_path / "logs/run_evaluation/fixed"
    report_dir = root / "offline/owner__repo-1"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.json"
    report.write_text('{"owner__repo-1":{"resolved":"false"}}')
    assert _report_resolved(report, "owner__repo-1") is None
    adapter = SWEBenchAdapter("verified")
    monkeypatch.setattr(adapter, "_check_module", lambda _: (True, "", ""))
    monkeypatch.setattr(adapter, "_check_docker", lambda: (True, "", ""))
    monkeypatch.setattr(adapter, "_prepull_instance_images", lambda *_a, **_k: ["owner__repo-1", "owner__repo-2", "owner__repo-4"])
    monkeypatch.setattr("nz_coder.swebench.adapter.subprocess.run",
        lambda cmd: subprocess.CompletedProcess(cmd, 0))
    args = SimpleNamespace(profile="verified", split="test", dataset_file=None,
        instance_ids=[f"owner__repo-{i}" for i in range(1, 5)], prepull_timeout=1,
        skip_prepull_failures=True, max_workers=1, run_id="fixed", timeout=60,
        clean=False, image_namespace="swebench", image_arch="x86_64", instance_image_tag="latest")
    assert adapter.run_harness(predictions, args) == 0
    summary = json.loads((root / "nz-evaluation-results.json").read_text())
    assert summary["counts"]["planned"] == 4
    assert summary["counts"]["resolved"] == summary["counts"]["unresolved"] == 0
    assert summary["counts"]["unknown"] == 2
    assert summary["counts"]["environment_blocked"] == 1
    assert summary["counts"]["empty_patch"] == 1
    assert all(row["official_resolved"] is None for row in summary["instances"])


def test_swe_timeout_stops_real_bash_descendant(tmp_path):
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.trace import TraceRecorder
    from nz_coder.swebench.orchestrator import AgentRunTimeout, _run_agent_attempt
    from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import os,time\nfrom pathlib import Path\n"
        "def test_slow():\n"
        "    Path('owned.pid').write_text(str(os.getpid()))\n"
        "    for _ in range(2000):\n"
        "        with open('pulse.txt','a') as f: f.write('pulse\\n'); f.flush()\n"
        "        time.sleep(.02)\n")
    traces = tmp_path / ".nz-coder-runs"
    traces.mkdir()
    (traces / "offline-actions.json").write_text(json.dumps([
        [["bash", {"command": "python3 -m pytest -q tests/test_slow.py", "timeout": 60}]]]))
    tracer = TraceRecorder(trace_dir=traces, session_id="timeout-regression")
    try:
        with scoped_workdir(tmp_path), scoped_runtime_overrides(max_agent_turns=2, strict_local_tools=True):
            with pytest.raises(AgentRunTimeout):
                _run_agent_attempt(offline_environment, "Use the test.", tracer,
                    [{"role": "user", "content": "Run python3 -m pytest -q tests/test_slow.py"}],
                    lambda *_a: None, 4,
                    agent_kwargs={"session_id": "timeout-regression", "tool_allowlist": STRICT_ALLOWED_TOOLS})
        pulse = tmp_path / "pulse.txt"
        assert pulse.is_file(), "real test subprocess must have started"
        before = pulse.stat().st_size
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline:
            time.sleep(.01)
            assert pulse.stat().st_size == before, "tool descendant still writing after worker timeout"
        requests = (traces / "actual-provider-requests.jsonl").read_text().splitlines()
        assert len(requests) == 1
    finally:
        pidfile = tmp_path / "owned.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text())
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_real_swe_runner_evidence_edit_retest_and_hidden_projection(repository, tmp_path, monkeypatch):
    from functools import partial
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.state.trace import TraceRecorder
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail
    from nz_coder.swebench.policy import validate_strict_tool_names

    (repository / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_value.py").write_text(
        "from source import VALUE\n"
        "def test_value():\n"
        "    if VALUE != 2:\n"
        "        for i in range(20000): print('ordinary diagnostic line', i)\n"
        "    assert VALUE == 2, 'UNIQUE ACTUAL FAILURE'\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "public existing test")
    monkeypatch.setattr("nz_coder.swebench.orchestrator._ensure_repo_cache", lambda *_a: repository)
    instance = {"instance_id": "owner__repo-1", "repo": "owner/repo",
        "base_commit": git(repository, "rev-parse", "HEAD").strip(),
        "problem_statement": "Fix source.py so VALUE is 2. Run python3 -m pytest -q -s tests/test_value.py.",
        "patch": "HIDDEN_GOLD_SENTINEL", "test_patch": "HIDDEN_TEST_SENTINEL",
        "hints_text": "HIDDEN_HINT_SENTINEL", "FAIL_TO_PASS": "HIDDEN_SELECTOR_SENTINEL"}
    actions = [
        [["repo_context", {"operation": "overview", "limit": 3}]],
        [["read_file", {"path": "source.py"}]],
        [["bash", {"command": "python3 -m pytest -q -s tests/test_value.py"}]],
        "READ_PREVIOUS_ARTIFACT",
        [["edit_file", {"path": "source.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]],
        [["bash", {"command": "python3 -m pytest -q -s tests/test_value.py"}]],
        "The targeted test passed for the current code."
    ]
    with scoped_runtime_overrides(max_agent_turns=7, repo_retrieval_strategy="policy"):
        result = RetryOrchestrator(SWEBenchAdapter("verified"), PatchGuardrail()).run_instance(
            instance, None, work_root=tmp_path / "runs", run_id="offline-production",
            config=None, build_prompt=lambda: "Use actual repository evidence. Report uncertainty.",
            agent_cls=partial(offline_environment, actions=actions), trace_cls=TraceRecorder,
            clone_timeout=30, agent_timeout=30, strict=True)
    work = Path(result["workdir"])
    requests = [json.loads(line) for line in (work / ".nz-coder-runs" / "actual-provider-requests.jsonl").read_text().splitlines()]
    assert not any("HIDDEN_" in json.dumps(request) for request in requests)
    main = [row["request"] for row in requests if row["role"] == "main"]
    schemas = {tool["function"]["name"] for request in main for tool in request.get("tools", [])}
    assert {"repo_context", "read_tool_result"} <= schemas
    assert validate_strict_tool_names(list(schemas)) == []
    assert "UNIQUE ACTUAL FAILURE" in json.dumps(main[4]["messages"])
    facts = [json.loads(line) for line in (work / ".nz-coder-runs" / "execution-facts.jsonl").read_text().splitlines()]
    tools = [row["result"] for row in facts if row["event"] == "tool_execution_result"]
    assert any(row["name"] == "repo_context" and row["executed"] and not row["dispatch_failed"] for row in tools)
    assert any(row["name"] == "read_tool_result" and row["executed"] and not row["dispatch_failed"] for row in tools)
    tests = [row for row in tools if row["name"] == "bash"]
    assert tests[0]["executed"] and tests[0]["command_failed"] and not tests[0]["dispatch_failed"]
    assert tests[-1]["executed"] and not tests[-1]["command_failed"]
    assert result["patch_status"] == "present"
    assert (work / "source.py").read_text() == "VALUE = 2\n"
    independent = subprocess.run(["python3", "-m", "pytest", "-q", "tests/test_value.py"],
        cwd=work, capture_output=True, text=True)
    assert independent.returncode == 0, independent.stdout + independent.stderr
    assert result["initial_state"]["original_tree"] == result["initial_state"]["local_tree"]
    assert result["initial_state"]["local_head"] != result["initial_state"]["original_base_commit"]


def test_actual_request_interrupted_before_journal_commit_does_not_restart(repository, tmp_path, monkeypatch):
    from functools import partial
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.state.trace import TraceRecorder
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail

    monkeypatch.setattr("nz_coder.swebench.orchestrator._ensure_repo_cache", lambda *_a: repository)
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    original_record = journal.record
    monkeypatch.setattr(journal, "record", lambda _: (_ for _ in ()).throw(OSError("interrupted before result fsync")))
    runner = RetryOrchestrator(SWEBenchAdapter("verified"), PatchGuardrail())
    instance = {"instance_id": "owner__repo-1", "repo": "owner/repo",
                "base_commit": git(repository, "rev-parse", "HEAD").strip(),
                "problem_statement": "Change source.py VALUE to 2."}
    actions = [[["read_file", {"path": "source.py"}]],
        [["edit_file", {"path": "source.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]]]
    kwargs = dict(work_root=tmp_path / "runs", run_id="interrupted-real", config=None,
        build_prompt=lambda: "Use actual evidence.", agent_cls=partial(offline_environment, actions=actions),
        trace_cls=TraceRecorder, clone_timeout=30, agent_timeout=15, empty_patch_retries=0,
        pred_file=None, model_name="offline", strict=True, attempt_journal=journal, cleanup_worktrees=True)
    with scoped_runtime_overrides(max_agent_turns=2):
        with pytest.raises(OSError, match="before result fsync"):
            runner.run_batch([instance], **kwargs)
    work = kwargs["work_root"] / instance["instance_id"]
    requests = work / ".nz-coder-runs" / "actual-provider-requests.jsonl"
    first_requests = requests.read_bytes()
    first_trace = {p.name: p.read_bytes() for p in requests.parent.glob("*.jsonl")}
    assert len(first_requests.splitlines()) == 2
    assert (work / "source.py").read_text() == "VALUE = 2\n"
    monkeypatch.setattr(journal, "record", original_record)
    result = runner.run_batch([instance], **kwargs)
    assert result[0]["status"] == "interrupted"
    assert requests.read_bytes() == first_requests
    assert first_trace == {p.name: p.read_bytes() for p in requests.parent.glob("*.jsonl")}
    assert (work / "source.py").read_text() == "VALUE = 2\n"
    assert journal.completed_ids() == set()


def test_real_provider_failure_keeps_committed_edit(repository, tmp_path, monkeypatch):
    from functools import partial
    from nz_coder.foundation import config
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.state.trace import TraceRecorder
    from nz_coder.swebench.adapter import SWEBenchAdapter
    from nz_coder.swebench.guardrail import PatchGuardrail

    monkeypatch.setattr("nz_coder.swebench.orchestrator._ensure_repo_cache", lambda *_a: repository)
    actions = [[["read_file", {"path": "source.py"}]],
        [["edit_file", {"path": "source.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]],
        "RAISE_PROVIDER"]
    instance = {"instance_id": "owner__repo-1", "repo": "owner/repo",
        "base_commit": git(repository, "rev-parse", "HEAD").strip(),
        "problem_statement": "Change source.py VALUE to 2. Then check the result."}
    predictions = tmp_path / "predictions.jsonl"
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    with scoped_runtime_overrides(max_agent_turns=3, nominal_agent_turns=3, strict_local_tools=True):
        results = RetryOrchestrator(SWEBenchAdapter(), PatchGuardrail()).run_batch(
            [instance], work_root=tmp_path / "runs", run_id="failed-provider",
            config=config, build_prompt=lambda: "Make the requested local edit.",
            agent_cls=partial(offline_environment, actions=actions), trace_cls=TraceRecorder,
            clone_timeout=10, agent_timeout=15, strict=True, empty_patch_retries=0,
            pred_file=None, model_name="offline", attempt_journal=journal,
            predictions_path=predictions)
    result = results[0]
    assert result["status"] == "agent_failed"
    assert "+VALUE = 2" in json.loads(predictions.read_text())["model_patch"]
    assert "+VALUE = 2" in result["model_patch"]
    assert result["patch_status"] == "present"
    assert result["official_resolved"] is None
    facts = [json.loads(line) for line in (Path(result["workdir"]) / ".nz-coder-runs/execution-facts.jsonl").read_text().splitlines()]
    edits = [row for row in facts if row["event"] == "tool_execution_result" and row["result"]["name"] == "edit_file"]
    assert len(edits) == 1 and edits[0]["result"]["executed"]


@pytest.mark.skipif(os.environ.get("NZ_SWE_DOCKER_SMOKE") != "1",
                    reason="requires explicit local Docker smoke with nz-swe-runtime:20261010; never downloads an image")
def test_real_cli_spawn_main_and_auxiliary_use_same_local_budget(tmp_path):
    import hashlib
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from nz_coder.evaluation.model_relay import InputAccounting, RelayBinding, RelayLedger, RelayLimits, RelayServer
    from nz_coder.swebench.profiles import get_profile

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("existing_review_launcher", root / "scripts/review_effects_execution.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    code = tmp_path / "code-new" / "nz_coder"
    shutil.copytree(root / "nz_coder", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for child in (code / "evaluation").iterdir():
        if child.name not in {"__init__.py", "reproducibility.py"}:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    import uuid
    case = tmp_path / ("cli-spawn-" + uuid.uuid4().hex[:8])
    for name in ("workspace", "home", "tmp", "input", "sockets", "result", "control", "driver"):
        (case / name).mkdir(parents=True)
    base = case / "workspace" / ".nz-coder" / "swebench-lite" / "repo-cache" / "owner_repo.git"
    source = tmp_path / "public-base"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "offline@example.invalid")
    git(source, "config", "user.name", "Offline")
    (source / "source.py").write_text("VALUE = 1\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "public initial state")
    base.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(base)], check=True, capture_output=True)
    (case / "input" / "instances.json").write_text(json.dumps({"dataset": get_profile("verified").dataset,
        "revision": "a" * 40, "split": "test", "instances": [{"instance_id": "owner__repo-1", "repo": "owner/repo",
        "base_commit": git(source, "rev-parse", "HEAD").strip(),
        "problem_statement": "Read source.py, then compact the conversation. Report observations without editing."}]}))
    shutil.copyfile(root / "tests/evaluation/fixtures/swebench_relay_entry.py", case / "driver" / "entry.py")
    requests = []

    class FakeBoundary(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            requests.append(request)
            schemas = [tool["function"]["name"] for tool in request.get("tools", [])]
            if not schemas:
                message = {"role": "assistant", "content": "Task: read source.py and report. Observed VALUE = 1; no edits or tests."}
            else:
                calls = [call for msg in request["messages"] for call in msg.get("tool_calls", [])]
                if not calls:
                    tool, arguments = "read_file", {"path": "source.py"}
                else:
                    assert "VALUE = 1" in json.dumps(request["messages"])
                    tool, arguments = "compact", {}
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "offline-" + str(len(requests)),
                    "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
            body = json.dumps({"id": "local-test", "created": 0, "model": request["model"], "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": len(raw), "completion_tokens": 4, "total_tokens": len(raw) + 4}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    def counter(raw, _request):
        return InputAccounting(len(raw), "offline-json-byte-test-count", trusted=True, exact=True,
            evidence_level="exact", contract_id="local-only", payload_sha256=hashlib.sha256(raw).hexdigest())

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBoundary)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    ledger = RelayLedger(case / "ledger.jsonl", RelayLimits(requests=3, input_total=300000, output_total=100000))
    servers = []
    for role, cap in (("main", 2), ("auxiliary", 1)):
        server = RelayServer(case / "sockets" / (role + ".sock"), ledger=ledger,
            binding=RelayBinding("offline-swe-smoke", "owner__repo-1", "same-attempt", role, cap,
                "deepseek-v4-flash", 8000, time.monotonic() + 60, role == "main"),
            upstream=f"http://127.0.0.1:{fake.server_port}/v1/chat/completions", counter=counter,
            trace_directory=case / "private-provider")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    argv = launcher.launch_argv(tmp_path, case, image="nz-swe-runtime:20261010")
    argv.insert(2, "--pull=never")
    argv += ["run-agent", "--profile", "verified", "--instances-file", "/input/instances.json",
        "--run-id", "offline-spawn", "--work-root", "/workspace/runs", "--output", "/result/predictions.jsonl",
        "--clone-timeout", "10", "--agent-timeout", "30", "--no-cleanup-worktrees",
        "--trace-budget-gib", "0.02", "--trace-warning-gib", "0.015", "--trace-cleanup-target-gib", "0.01"]
    # 显式测试配置，不能继承真实密钥或代理；SWE 和所有辅助模型均经过同一账本。
    insert = argv.index("nz-swe-runtime:20261010")
    argv[insert:insert] = ["--env", "MAX_AGENT_TURNS=5", "--env", "NZ_SWE_NOMINAL_AGENT_TURNS=5",
                         "--env", "MAX_OUTPUT_TOKENS=8000", "--env", "NZ_PROVIDER_MAX_RETRIES=0"]
    container = "nz-review-" + case.name
    try:
        completed = subprocess.run(argv, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                                   capture_output=True, text=True, timeout=50)
        (case / "stdout.txt").write_text(completed.stdout)
        (case / "stderr.txt").write_text(completed.stderr)
        assert completed.returncode == 1, completed.stdout + completed.stderr
        events = [json.loads(line) for line in (case / "ledger.jsonl").read_text().splitlines()]
        admitted = [row for row in events if row["event"] == "admitted"]
        assert [row["role"] for row in admitted] == ["main", "main", "auxiliary"]
        assert any(row.get("reason") == "physical_request_cap_exhausted" for row in events)
        alternate = list(argv)
        alternate[alternate.index("--name") + 1] = container + "-owner-check"
        alternate[alternate.index("/result/predictions.jsonl")] = "/result/alternate.jsonl"
        rejected = subprocess.run(alternate, env={"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                                  capture_output=True, text=True, timeout=20)
        assert rejected.returncode == 2
        assert "work root belongs to another attempt/output" in rejected.stdout
        assert not (case / "result" / "alternate.jsonl").exists()
        assert len(requests) == 3
        assert all(row["run"] == "same-attempt" for row in admitted)
        report = json.loads((case / "result" / "predictions.report.json").read_text())
        assert report[0]["agent_status"]["status"] == "aborted"
        assert report[0]["official_resolved"] is None
        assert (case / "workspace" / "runs" / "owner__repo-1" / "source.py").read_text() == "VALUE = 1\n"
    finally:
        ledger.cancel("same-attempt")
        subprocess.run(["docker", "rm", "-f", container, container + "-owner-check"], capture_output=True)
        for server in servers:
            server.shutdown()
            server.server_close()
        fake.shutdown()
        fake.server_close()
        ledger.close()
