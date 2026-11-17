"""每个自然候选都实际审查，结构化结果和历史失败也属于审查身份。"""
from __future__ import annotations

from test_delivery_assessment_runner import accepted, run_probe
from test_completion_pruning_domain import domain
from nz_coder.runtime.verification.domain.models import ExecutionFact, candidate_fingerprint
from nz_coder.runtime.verification.artifact_evidence import generic_requirement_materials
from nz_coder.runtime.verification.sidecar_verifier import SidecarVerifierHook, VerifierGateMetrics, build_verifier_context


def test_one_line_document_change_is_actually_reviewed(monkeypatch, tmp_path):
    result, _hook, provider, snapshot = run_probe(monkeypatch, tmp_path,
        reports=lambda *_: (accepted(), "tool_calls"), candidate="README.md documents the usage.",
        task="Create README.md with usage instructions.", always=False,
        initial_actions=([("write_file", {"path": "README.md", "content": "# Usage\nRun the application.\n"})],))
    assert provider.reviews == 1
    assert result.status.value == "completed", (result.error, result.final_text)
    assert snapshot.reviews


def test_structured_candidate_changes_cache_identity():
    context = build_verifier_context([{"role": "user", "content": "Return statistics"}],
                                     "The result.", file_edits=[])
    first = SidecarVerifierHook._accepted_cache_key(context, VerifierGateMetrics(),
        {"verification": {"candidate_fingerprint": candidate_fingerprint("The result.", {"count": 1})}})
    second = SidecarVerifierHook._accepted_cache_key(context, VerifierGateMetrics(),
        {"verification": {"candidate_fingerprint": candidate_fingerprint("The result.", {"count": 999})}})
    assert first != second


def test_unknown_command_failure_survives_without_document_or_test_artifact(tmp_path):
    from nz_coder.runtime.verification.domain.identity import command_identity

    owner = domain(tmp_path)
    owner.observe_execution(ExecutionFact("unknown-failure", owner.scope,
        command_identity("./custom-check", str(tmp_path)), "failed", owner.snapshot().version,
        owner.snapshot().version, exit_code=19, reason="specific failed assertion"))
    text, digest = generic_requirement_materials(owner.snapshot(), tmp_path)
    assert "unknown-failure" in text
    assert "specific failed assertion" in text
    assert digest


def test_live_domain_reuses_identical_review_without_self_invalidating(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider
    owner = domain(tmp_path)
    calls = []
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(name='emit_sidecar_verdict', arguments=json.dumps(accepted())))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]))])
    loop = SimpleNamespace(workdir=tmp_path, change_tracker=None, tracer=None,
        verification_runtime=SimpleNamespace(domain=owner, reconcile_workspace=lambda: None),
        _model_gateway_observer_for_run=lambda: None)
    hook = SidecarVerifierHook(loop, ResolvedVerifierProvider(Provider(), object(), 'offline', 'offline', 'test'), env={})
    context = StopHookContext(transcript=({'role': 'user', 'content': 'Explain the result'},),
        last_assistant_text='The requested explanation.', runtime_state={})
    for _ in range(3):
        assert asyncio.run(hook(context)).action == 'complete'
    assert len(calls) == 1
