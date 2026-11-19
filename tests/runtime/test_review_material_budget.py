"""完整评审材料由实际模型预算约束，编译缓存不充当测试产物。"""
from __future__ import annotations

import asyncio
import hashlib
import py_compile
from pathlib import Path

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.verification.domain.models import ArtifactFact
from test_generic_requirement_evidence import _pass, _setup, _sidecar, _write


def _observe(domain, root, path):
    domain.observe_artifact(ArtifactFact(domain.scope.task, path, "create", 0,
        hashlib.sha256((root / path).read_bytes()).hexdigest()))


@pytest.mark.parametrize("restored", [False, True])
def test_real_compiled_test_cache_is_not_reviewed_as_source(tmp_path, restored):
    from dataclasses import replace
    import threading

    from nz_coder.foundation.file_kinds import is_test_file
    from nz_coder.runtime.adapters.verification_workspace import collect_artifact_facts
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    cached = Path(py_compile.compile(str(tmp_path / "tests/test_cli.py"), doraise=True))
    relative = cached.relative_to(tmp_path).as_posix()
    _observe(domain, tmp_path, relative)
    if restored:
        domain = restore_domain(domain.checkpoint(), lock=threading.RLock(),
            scope=replace(domain.scope, interaction_run_id="continued"), version=domain.snapshot().version)
        for fact in collect_artifact_facts(domain.snapshot(), tmp_path, {}, 0):
            domain.observe_artifact(fact)
    _pass(domain, spec, "new-pass")
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    decision = asyncio.run(hook(context))
    assert decision.action == "complete", decision.message
    assert requests and "def test_ok(self)" in str(requests[0]["messages"])
    assert relative not in str(requests[0]["messages"])
    assert domain.snapshot().ready_for_completion
    assert is_test_file(relative)  # 权限所用的全局测试路径分类保持原义。


@pytest.mark.parametrize("path", ["tests/test_cli.pyc", "tests/test_cli.pyo", "tests/__pycache__/test_cli.py",
                                  "tests/TEST_CLI.PYC", "tests\\__PYCACHE__\\test_cli.py"])
def test_cache_alone_cannot_prove_generic_test_modification(tmp_path, path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path, (path, "README.md"))
    _pass(domain, spec)
    progress = next(p for p in domain.snapshot().requirements if p.requirement.id == "R2")
    # 同代单测通过提供了部分证据，缓存仍不能充当新增测试。
    assert progress.status == "candidate" and not progress.deterministic_ready
    assert not any(e.type in {"file_created", "file_changed"} for e in progress.evidence)


def test_complete_materials_above_64k_reach_model_without_truncation(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    path = tmp_path / "README.md"
    path.write_text("Detailed usage instructions.\n" * 5000 + "\nEND_OF_COMPLETE_DOCS\n")
    _observe(domain, tmp_path, "README.md")
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    hook._resolved.provider.capabilities = lambda model: ModelCapabilities(
        provider="offline-review", model_id=model, context_tokens=1_048_576)
    decision = asyncio.run(hook(context))
    assert decision.action == "complete", decision.message
    assert len(requests) == 1
    assert "END_OF_COMPLETE_DOCS" in str(requests[0]["messages"])
    assert "def test_ok(self)" in str(requests[0]["messages"])
    assert domain.snapshot().ready_for_completion


def test_complete_cjk_request_over_budget_does_not_call_provider(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    path = tmp_path / "README.md"
    path.write_text("使用说明" * 10000)
    _observe(domain, tmp_path, "README.md")
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    hook._resolved.provider.capabilities = lambda model: ModelCapabilities(
        provider="offline-review", model_id=model, context_tokens=16_000)
    decision = asyncio.run(hook(context))
    assert decision.action == "abort"
    assert "request" in decision.message.casefold() and "budget" in decision.message.casefold()
    assert not requests
    assert not domain.snapshot().ready_for_completion


def test_report_tool_schema_counts_toward_complete_request_budget(tmp_path, monkeypatch):
    from nz_coder.runtime.verification import sidecar_verifier

    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    hook._resolved.provider.capabilities = lambda model: ModelCapabilities(
        provider="offline-review", model_id=model, context_tokens=16_000)
    monkeypatch.setitem(sidecar_verifier.VERIFIER_REPORT_TOOL, "description", "审查规则" * 5000)
    decision = asyncio.run(hook(context))
    assert decision.action == "abort" and "request exceeds input budget" in decision.message
    assert requests == []
    assert not domain.snapshot().ready_for_completion


@pytest.mark.parametrize("boundary", ["natural_completion", "tool_batch"])
@pytest.mark.parametrize("pending_after", [False, True])
@pytest.mark.parametrize("at_hard_cap", [False, True])
def test_review_abort_keeps_reason_without_completion_reanimation(tmp_path, boundary, pending_after, at_hard_cap):
    from types import SimpleNamespace

    from nz_coder.runtime.execution.runner import AgentRunner
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.execution.work_budget import WorkBudgetController
    from nz_coder.runtime.verification.domain.models import WorkspaceObservation, WorkspaceVersion
    from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision

    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    state = RuntimeState()
    state.bind_run_context(SimpleNamespace(verification=domain, finalized=False, _state_lock=domain._lock))
    state.set_task_contract(domain.task_definition.contract)
    calls = []
    reason = "Runtime review evidence unavailable: complete review request exceeds input budget"

    def abort(_context):
        calls.append(reason)
        if pending_after:
            domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
        return StopHookDecision(action="abort", message=reason, source="verification-evidence")

    hooks = AgentHooks(completion_review_hooks=[abort])
    host = SimpleNamespace(hooks=hooks, runtime_state=state)

    async def review(messages, content, *, review, guidance):
        return await hooks.handle_delivery_boundary_async(host, messages, message=content,
                                                         review=review, guidance=guidance)

    async def checkpoint(*_args):
        pass

    context = SimpleNamespace(runtime_state=state,
        policy=SimpleNamespace(verification_status=lambda: domain.snapshot().to_dict(),
                               prepare_completion=lambda: None, settle_completion=review),
        hooks=SimpleNamespace(trace=lambda *_args, **_kwargs: None),
        control=SimpleNamespace(persist_runtime_state=lambda **_kwargs: None,
                                structured_output=lambda: None, invalidate_completion_payload=lambda: None,
                                stop_hook_reason=lambda: hooks.stop_hook_reason))
    messages = [{"role": "user", "content": "Add tests and docs"},
                {"role": "assistant", "content": "All done"}]
    budget = WorkBudgetController(max_turns=20, nominal_turns=10)
    decision = asyncio.run(AgentRunner()._settle_terminal_boundary(
        context, SimpleNamespace(session_runtime=SimpleNamespace(checkpoint=checkpoint)),
        SimpleNamespace(request=SimpleNamespace(workspace=tmp_path), interaction_run_id="run",
                        verification=domain, metadata={}, check_cancelled=lambda: None),
        messages, boundary=boundary, content="All done", completed_turns=20 if at_hard_cap else 10,
        work_budget=budget, resolve_tool_runtime_context=lambda: None,
        natural_completion=boundary == "natural_completion"))
    if boundary == "natural_completion" and not pending_after:
        assert calls == [reason]
        assert decision.action == "finalize" and decision.status == "blocked"
        assert decision.content == reason
    elif boundary == "natural_completion":
        assert calls == [reason] and hooks.stop_hook_reason == reason
        assert decision.reason == ("hard_cap_exhausted" if at_hard_cap else "candidate_invalidated")
    else:
        # 一批工具结束后没有提交交付候选，主模型获取结果；耗尽轮数时如实退出。
        assert not calls
        assert decision.action == ("finalize" if at_hard_cap else "continue")
        if at_hard_cap:
            assert decision.status == "max_turns"
    assert len(messages) == 2 and state.completion_gate_prompts == 0


def test_explicit_cached_directory_deliverables_are_identified_individually(tmp_path):
    import json
    from dataclasses import replace
    from nz_coder.runtime.verification.artifact_evidence import generic_requirement_materials

    text = "tests/__pycache__/README.md"
    binary = "output.pyo"
    incidental = "tests/test_unrequested.pyc"
    domain, _spec = _setup(tmp_path)
    definition = domain.task_definition
    contract = definition.contract
    required = replace(contract.requirements[0], expected_artifacts=(text, binary))
    domain.declare_task(replace(definition, contract=replace(contract, requirements=(required,))))
    for path, contents in [(text, b"Documented behavior"), (binary, b"\xff\x00"), (incidental, b"\xff\x01")]:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        _observe(domain, tmp_path, path)
    materials, _ = generic_requirement_materials(domain.snapshot(), tmp_path)
    rows = [json.loads(line) for line in materials.splitlines() if line.startswith("{")]
    records = {row["path"]: row for row in rows}
    assert set(records) == {text, binary}
    assert records[text]["content"] == "Documented behavior"
    receipt = records[binary]
    assert receipt["sha256"] == hashlib.sha256((tmp_path / binary).read_bytes()).hexdigest()
    assert receipt["size_bytes"] == 2 and receipt["evidence"].startswith("binary identity only")
    assert "content" not in receipt
    (tmp_path / text).unlink()
    domain.observe_artifact(ArtifactFact(domain.scope.task, text, "delete", 0, "deletion", present=False))
    materials, _ = generic_requirement_materials(domain.snapshot(), tmp_path)
    assert json.loads(next(line for line in materials.splitlines() if text in line))["operation"] == "delete"
