"""评测入口的离线安全与评分回归；危险删除永远拦截。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from nz_coder.swebench import adapter as adapter_mod, cli, orchestrator
from nz_coder.swebench.profiles import get_profile


def instance(instance_id="owner__repo-1"):
    return dict(
        instance_id=instance_id,
        repo="owner/repo",
        base_commit="a" * 40,
        problem_statement="public issue",
    )


@pytest.mark.parametrize(
    "bad",
    [".", "..", "../outside", "/outside", "a/b", "a\\b", "C:relative", "a b", "a?b"],
)
def test_frozen_and_direct_dispatch_reject_unsafe_ids_before_side_effects(
    tmp_path, monkeypatch, bad
):
    source = tmp_path / "instances.json"
    source.write_text(
        json.dumps(
            dict(
                dataset=get_profile("verified").dataset,
                revision="a" * 40,
                split="test",
                instances=[instance(bad)],
            )
        )
    )
    with pytest.raises(ValueError):
        cli.load_inference_instances(source, get_profile("verified"), "test")
    monkeypatch.setattr(
        orchestrator, "_prepare_repo", lambda *_a, **_k: pytest.fail("prepare invoked")
    )
    with pytest.raises(ValueError):
        orchestrator.RetryOrchestrator(None, None).run_instance(
            instance(bad),
            None,
            work_root=tmp_path / "work",
            run_id="offline",
            config=None,
            build_prompt=None,
            agent_cls=None,
            trace_cls=None,
            clone_timeout=1,
            agent_timeout=1,
        )
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize(
    "target",
    [
        "root",
        "parent",
        "outside",
        "sibling_link",
        "outside_link",
        "dangling_link",
        "file",
    ],
)
@pytest.mark.parametrize("entry", ["prepare", "cleanup"])
def test_worktree_boundaries_reject_before_delete_or_clone(
    tmp_path, monkeypatch, target, entry
):
    root = tmp_path / "work"
    root.mkdir()
    sibling = root / "other"
    sibling.mkdir()
    sentinel = sibling / "KEEP"
    sentinel.write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "KEEP").write_text("keep")
    candidate = root / "one"
    if target == "root":
        candidate = root
    elif target == "parent":
        candidate = root.parent
    elif target == "outside":
        candidate = outside
    elif target.endswith("link"):
        candidate.symlink_to(
            sibling
            if target == "sibling_link"
            else outside
            if target == "outside_link"
            else tmp_path / "missing",
            target_is_directory=True,
        )
    else:
        candidate.write_text("file")
    monkeypatch.setattr(
        orchestrator.shutil, "rmtree", lambda *_a, **_k: pytest.fail("unsafe rmtree")
    )
    monkeypatch.setattr(
        orchestrator, "_ensure_repo_cache", lambda *_a: pytest.fail("unsafe clone")
    )
    with pytest.raises(ValueError):
        if entry == "prepare":
            orchestrator._prepare_repo(instance(), candidate, 1, work_root=root)
        else:
            orchestrator._cleanup_completed_worktree(candidate, root)
    assert sentinel.read_text() == (outside / "KEEP").read_text() == "keep"


def test_prepare_clone_retry_rechecks_link_before_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    other = root / "other"
    other.mkdir()
    (other / "KEEP").write_text("keep")
    repo = root / "one"
    monkeypatch.setattr(
        orchestrator, "_ensure_repo_cache", lambda *_a: tmp_path / "cache.git"
    )

    def clone(cmd, **_kwargs):
        repo.symlink_to(other, target_is_directory=True)
        return subprocess.CompletedProcess(cmd, 1, "", "failed")

    monkeypatch.setattr(orchestrator, "_run", clone)
    monkeypatch.setattr(
        orchestrator.shutil,
        "rmtree",
        lambda *_a, **_k: pytest.fail("unsafe retry cleanup"),
    )
    with pytest.raises(ValueError):
        orchestrator._prepare_repo(instance(), repo, 1, work_root=root)
    assert (other / "KEEP").read_text() == "keep"


def test_valid_worktree_repeated_prepare_retry_and_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "work"
    repo = root / "one"
    monkeypatch.setattr(
        orchestrator, "_ensure_repo_cache", lambda *_a: tmp_path / "cache.git"
    )
    clone_count = 0

    def fake_git(cmd, **_kwargs):
        nonlocal clone_count
        if cmd[:2] == ["git", "clone"]:
            clone_count += 1
            repo.mkdir()
            (repo / "data").write_text(str(clone_count))
            if clone_count == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "cache failed")
        return subprocess.CompletedProcess(cmd, 0, "a" * 40, "")

    monkeypatch.setattr(orchestrator, "_run", fake_git)
    for _ in range(2):
        assert (
            orchestrator._prepare_repo(instance(), repo, 1, work_root=root)[
                "returncode"
            ]
            == 0
        )
    assert (repo / "data").read_text() == "3"
    orchestrator._cleanup_completed_worktree(repo, root)
    orchestrator._cleanup_completed_worktree(repo, root)
    assert root.is_dir() and not repo.exists()


def test_production_behavior_reads_real_private_session_trace(tmp_path):
    from nz_coder.evaluation.behavioral import ProductionAgentBehaviorDriver
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.state.sessions import session_trace_dir
    from nz_coder.state.trace import TraceRecorder

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with scoped_workdir(workspace):
        recorder = TraceRecorder(
            trace_dir=session_trace_dir("behavior-events"), session_id="behavior-events"
        )
        recorder.log("llm_response", input_tokens=17, output_tokens=3)
    rows = ProductionAgentBehaviorDriver._trace_events(workspace, "behavior-events")
    assert any(
        row.get("event") == "llm_response" and row.get("input_tokens") == 17
        for row in rows
    )


def test_native_scenario_sessions_match_runtime_port():
    from nz_coder.evaluation.native_scenario import _Sessions
    from nz_coder.runtime.core.contracts import SessionRuntimePort

    assert isinstance(_Sessions(), SessionRuntimePort)


def test_aider_console_entrypoint_awaits_real_async_command(monkeypatch):
    from nz_coder.evaluation import aider_benchmark

    invoked = []

    async def run(args):
        invoked.append(args.exercise)
        return 7

    monkeypatch.setattr(aider_benchmark, "run_python_exercise", run)
    code = aider_benchmark.main(["run-python", "--exercise", "offline"])
    if asyncio.iscoroutine(code):
        code.close()
    assert code == 7
    assert invoked == ["offline"]


def harness_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps(
            dict(
                instance_id="owner__repo-1",
                model_name_or_path="org/model",
                model_patch="patch",
            )
        )
        + "\n"
    )
    adapter = adapter_mod.SWEBenchAdapter("lite")
    monkeypatch.setattr(
        adapter, "_check_module", lambda *_a: (True, "swebench", "offline")
    )
    monkeypatch.setattr(adapter, "_check_docker", lambda: (True, "docker", "offline"))
    monkeypatch.setattr(
        adapter_mod.importlib.metadata, "version", lambda *_a: "offline"
    )
    args = cli.build_parser().parse_args(
        [
            "run-eval",
            "--profile",
            "lite",
            "--predictions-path",
            str(predictions),
            "--run-id",
            "offline",
            "--prepull-timeout",
            "1",
            "--skip-prepull-failures",
        ]
    )
    return adapter, predictions, args


def test_all_prepull_failures_do_not_dispatch_harness(tmp_path, monkeypatch):
    adapter, predictions, args = harness_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, "_prepull_instance_images", lambda *_a, **_k: [])
    dispatched = []
    monkeypatch.setattr(
        adapter_mod.subprocess,
        "run",
        lambda cmd, **_k: dispatched.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert adapter.run_harness(predictions, args) == 3
    assert dispatched == []
    result = json.loads(
        (
            tmp_path / "logs/run_evaluation/offline/nz-evaluation-results.json"
        ).read_text()
    )
    assert (
        result["counts"]["attempted"] == 0
        and result["counts"]["environment_blocked"] == 1
    )


@pytest.mark.parametrize("selected", ["", "aarch64", "arm64"])
def test_native_aarch64_default_and_explicit_alias_share_image_arch(
    tmp_path, monkeypatch, selected
):
    adapter, predictions, args = harness_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter_mod.platform, "machine", lambda: "aarch64")
    args.image_arch = selected
    arches = []
    monkeypatch.setattr(
        adapter,
        "_prepull_instance_images",
        lambda ids, **kw: arches.append(kw["arch"]) or ids,
    )
    monkeypatch.setattr(
        adapter_mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0)
    )
    assert adapter.run_harness(predictions, args) == 0
    assert arches == ["arm64"]
    identity = json.loads(
        (
            tmp_path / "logs/run_evaluation/offline/nz-evaluation-identity.json"
        ).read_text()
    )
    assert identity["arch"] == "arm64"


def test_model_directory_encoding_matches_official_results_and_submission(
    tmp_path, monkeypatch
):
    from nz_coder.swebench.submission import _find_instance_log_dir

    adapter, predictions, args = harness_setup(tmp_path, monkeypatch)
    official = tmp_path / "logs/run_evaluation/offline/org__model/owner__repo-1"
    official.mkdir(parents=True)
    (official / "report.json").write_text(json.dumps({"resolved": True}))
    adapter._write_evaluation_results(
        predictions, "offline", ["owner__repo-1"], ["owner__repo-1"], 0
    )
    rows = json.loads(
        (official.parent.parent / "nz-evaluation-results.json").read_text()
    )["instances"]
    located = _find_instance_log_dir(
        official.parent.parent,
        "owner__repo-1",
        run_id="offline",
        model_name="org/model",
    )
    assert located == official
    assert Path(rows[0]["official_report"]).resolve() == located / "report.json"
    assert rows[0]["official_resolved"] is True


def test_write_tests_preserves_real_pytest_failure(tmp_path, monkeypatch):
    from nz_coder.evaluation import benchmark

    monkeypatch.setattr(benchmark, "BENCH_DIR", tmp_path)
    (tmp_path / "test_string_utils.py").write_text(
        "def test_one(): assert False\ndef test_two(): assert False\ndef test_three(): assert False\n"
    )
    result = benchmark.WriteTests().verify()
    assert result["passed"] is False
    assert "failed" in result["reason"].lower()


def test_write_tests_reports_missing_pytest_without_direct_execution(
    tmp_path, monkeypatch
):
    from nz_coder.evaluation import benchmark

    monkeypatch.setattr(benchmark, "BENCH_DIR", tmp_path)
    (tmp_path / "test_string_utils.py").write_text(
        "def test_one(): pass\ndef test_two(): pass\ndef test_three(): pass\n"
    )
    calls = []

    def missing(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 1, "", f"{sys.executable}: No module named pytest"
        )

    monkeypatch.setattr(benchmark.subprocess, "run", missing)
    result = benchmark.WriteTests().verify()
    assert (
        result["passed"] is False
        and "pytest" in result["reason"]
        and "unavailable" in result["reason"].lower()
    )
    assert len(calls) == 1


def test_linux_acceptance_tee_pipeline_preserves_pytest_failure():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/windows-product-rc.yml"
    )
    step = (
        workflow.read_text()
        .split("- name: Linux product acceptance", 1)[1]
        .split("- run:", 1)[0]
    )
    assert "shell: bash" in step or "set -o pipefail" in step
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", "exit 7 | tee /dev/null"],
        capture_output=True,
    )
    assert result.returncode == 7


@pytest.mark.parametrize("bad", [".", "..", "a/b", "a\\b"])
def test_public_run_agent_rejects_invalid_frozen_input_before_work_root(
    tmp_path, monkeypatch, bad
):
    frozen = tmp_path / "instances.json"
    frozen.write_text(
        json.dumps(
            dict(
                dataset=get_profile("lite").dataset,
                revision="a" * 40,
                split="test",
                instances=[instance(bad)],
            )
        )
    )
    work = tmp_path / "runs"
    output = tmp_path / "output/predictions.jsonl"
    args = cli.build_parser().parse_args(
        [
            "run-agent",
            "--profile",
            "lite",
            "--instances-file",
            str(frozen),
            "--work-root",
            str(work),
            "--output",
            str(output),
        ]
    )
    calls = []
    monkeypatch.setattr(
        cli, "_run_agent_owned", lambda *_a, **_k: calls.append("dispatch") or 0
    )
    assert cli.run_agent(args) == 2
    assert not calls and not work.exists() and not output.parent.exists()


@pytest.mark.parametrize("entry", ["prepare", "cleanup"])
def test_windows_reparse_attribute_rejected_before_resolving_or_deleting(
    tmp_path, monkeypatch, entry
):
    import stat

    root = tmp_path / "work"
    root.mkdir()
    repo = root / "one"
    repo.mkdir()
    (repo / "KEEP").write_text("keep")
    real_lstat = Path.lstat

    def reparse(path):
        value = real_lstat(path)
        return (
            SimpleNamespace(
                st_mode=value.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
            if path == repo
            else value
        )

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(
        orchestrator.shutil, "rmtree", lambda *_a, **_kw: pytest.fail("reparse delete")
    )
    with pytest.raises(ValueError, match="linked"):
        if entry == "prepare":
            orchestrator._prepare_repo(instance(), repo, 1, work_root=root)
        else:
            orchestrator._cleanup_completed_worktree(repo, root)
    assert (repo / "KEEP").read_text() == "keep"


@pytest.mark.parametrize("bad", [".", "..", "a/b", "a\\b", "a b"])
def test_direct_instance_dispatch_validates_before_prepare(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(
        orchestrator,
        "_prepare_repo",
        lambda *_a, **_kw: pytest.fail("invalid dispatch"),
    )
    with pytest.raises(ValueError):
        orchestrator.RetryOrchestrator(None, None).run_instance(
            instance(bad),
            None,
            work_root=tmp_path / "work",
            run_id="offline",
            config=None,
            build_prompt=None,
            agent_cls=None,
            trace_cls=None,
            clone_timeout=1,
            agent_timeout=1,
        )
    assert not (tmp_path / "work").exists()
