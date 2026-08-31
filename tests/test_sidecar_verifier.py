"""Source-translated contracts for the InfCodeX coding Sidecar Verifier."""
from __future__ import annotations


def test_verifier_context_keeps_real_query_and_bounds_recent_transcript():
    """Catches synthetic guidance replacing the ask or unbounded judge context."""
    from nz_coder.runtime.verification.sidecar_verifier import build_verifier_context

    transcript = [{"role": "user", "content": "fix parser"}]
    transcript += [{"role": "assistant", "content": str(index)} for index in range(30)]
    transcript += [{
        "role": "user",
        "content": "internal retry",
        "_nz_synthetic": True,
    }]
    context = build_verifier_context(
        transcript,
        "done",
        file_edits=[{"path": "parser.py", "diff_hint": "2 mutations"}],
    )

    assert context.current_turn_user_queries == ("fix parser",)
    assert len(context.recent_transcript) == 24
    assert all(message["role"] != "system" for message in context.recent_transcript)
    assert context.file_edit_summary == (("parser.py", "2 mutations"),)
    assert context.last_assistant_text == "done"


def test_verifier_user_message_uses_third_person_evidence_sections():
    """Catches passing Main Agent messages as the verifier's own history."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierContext,
        build_verifier_user_message,
    )

    rendered = build_verifier_user_message(VerifierContext(
        current_turn_user_queries=("fix parser",),
        recent_transcript=({
            "role": "assistant",
            "content": "I changed it",
            "tool_calls": [{
                "id": "edit",
                "function": {"name": "edit_file", "arguments": {"path": "parser.py"}},
            }],
        },),
        file_edit_summary=(("parser.py", "2 mutations"),),
        last_assistant_text="Completed.",
    ))

    assert "=== USER REQUEST (CURRENT TURN) ===\nfix parser" in rendered
    assert "[MAIN AGENT TEXT]: I changed it" in rendered
    assert "[MAIN AGENT TOOL]: edit_file" in rendered
    assert "=== FILE EDITS PERFORMED THIS TURN ===\n- parser.py: 2 mutations" in rendered
    assert "=== MAIN AGENT FINAL TEXT" in rendered
    assert rendered.count("Completed.") == 1


def test_gate_first_match_order_covers_substantial_and_trivial_work():
    """Catches metric branches firing in the wrong order or skipping risky work."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierGateMetrics,
        compose_gate_decision,
    )

    transcript = ({"role": "user", "content": "fix parser"},)
    forced = compose_gate_decision(
        transcript,
        VerifierGateMetrics(),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )
    risky = compose_gate_decision(
        transcript,
        VerifierGateMetrics(risky_shell_ops=1),
        env={},
    )
    planned = compose_gate_decision(
        transcript,
        VerifierGateMetrics(has_plan=True),
        env={},
    )
    long_run = compose_gate_decision(
        transcript,
        VerifierGateMetrics(rounds=11, any_tool_use=True),
        env={},
    )
    multi_file = compose_gate_decision(
        transcript,
        VerifierGateMetrics(write_ops=2, files_changed=2),
        env={},
    )
    large_edit = compose_gate_decision(
        transcript,
        VerifierGateMetrics(write_ops=1, files_changed=1, estimated_changed_lines=21),
        env={},
    )
    trivial = compose_gate_decision(
        transcript,
        VerifierGateMetrics(write_ops=1, files_changed=1, estimated_changed_lines=2),
        env={},
    )

    assert forced == (True, "escape-hatch")
    assert risky == (True, "risky-shell")
    assert planned == (True, "has-plan")
    assert long_run == (True, "long-run")
    assert multi_file == (True, "multi-file")
    assert large_edit == (True, "large-edit")
    assert trivial == (False, "trivial-observed-work")


def test_gate_skips_greeting_but_fires_for_ungrounded_claim():
    """Catches the conversational skip swallowing a real action request."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierGateMetrics,
        compose_gate_decision,
    )

    greeting = compose_gate_decision(
        ({"role": "user", "content": "你好"},),
        VerifierGateMetrics(),
        env={},
    )
    imperative = compose_gate_decision(
        ({"role": "user", "content": "你好，检查代码"},),
        VerifierGateMetrics(),
        env={},
    )
    claim = compose_gate_decision(
        ({"role": "user", "content": "implement endpoint"},),
        VerifierGateMetrics(),
        env={},
    )

    assert greeting == (False, "conversational-intent")
    assert imperative == (True, "default-fire")
    assert claim == (True, "default-fire")


def test_gate_skips_grounded_history_report_but_not_ungrounded_claim():
    """Avoid paying for a verifier when a resumed turn only reports proven history."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierGateMetrics,
        compose_gate_decision,
    )

    prior_evidence = (
        {"role": "user", "content": "Fix normalize_query and run its tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "bash"}}],
        },
        {"role": "tool", "content": "2 passed in 0.00s"},
    )
    report_request = {
        "role": "user",
        "content": (
            "Continue this Session. Do not call tools and do not modify files. "
            "In one sentence, report the function changed and the exact "
            "verification result already obtained in the previous turn."
        ),
    }

    grounded = compose_gate_decision(
        prior_evidence + (report_request,),
        VerifierGateMetrics(),
        env={},
    )
    no_history = compose_gate_decision(
        (report_request,),
        VerifierGateMetrics(),
        env={},
    )
    new_action = compose_gate_decision(
        prior_evidence
        + (
            {
                "role": "user",
                "content": "Do not call tools or modify files; implement endpoint.",
            },
        ),
        VerifierGateMetrics(),
        env={},
    )

    assert grounded == (False, "grounded-history-report")
    assert no_history == (True, "default-fire")
    assert new_action == (True, "default-fire")


def test_verdict_parser_degrades_invalid_or_reasonless_blocking_results():
    """Catches malformed verifier output blocking or reanimating the Main Agent."""
    from nz_coder.runtime.verification.sidecar_verifier import parse_verifier_report

    assert parse_verifier_report(
        {"input": {"verdict": "REVISE", "reason": "Add import"}},
        True,
    ).as_dict() == {
        "verdict": "revise",
        "reason": "Add import",
        "suggested_fix": "",
        "trace": "verifier_ok",
    }
    assert parse_verifier_report(
        {"input": {"verdict": "blocked", "reason": ""}},
        True,
    ).as_dict()["trace"] == "missing_reason"
    assert parse_verifier_report(
        {"input": {"verdict": "maybe", "reason": "unclear"}},
        True,
    ).as_dict()["trace"] == "invalid_verdict_value"


def test_verdict_mapping_preserves_three_stop_states():
    """Catches accept/revise/blocked landing on the wrong Runner action."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierVerdict,
        map_verdict_to_stop_decision,
    )

    accept = map_verdict_to_stop_decision(VerifierVerdict("accept", "", trace="verifier_ok"))
    revise = map_verdict_to_stop_decision(
        VerifierVerdict("revise", "Add the missing import.", trace="verifier_ok")
    )
    blocked = map_verdict_to_stop_decision(
        VerifierVerdict("blocked", "Grant repository access.", trace="verifier_ok")
    )

    assert accept.action == "complete"
    assert revise.action == "reanimate"
    assert revise.source == "sidecar-verifier"
    assert "Add the missing import." in revise.message
    assert "failed Sidecar Verifier review" in revise.message
    assert blocked.action == "abort"
    assert blocked.message == "Grant repository access."


def test_provider_adapter_forces_only_verdict_tool_and_parses_response():
    """Catches the verifier receiving coding tools or an unforced free-form call."""
    from types import SimpleNamespace

    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierContext,
        invoke_sidecar_verifier,
    )

    class Provider:
        name = "fake"

        def __init__(self):
            self.requests = []

        def create_completion(self, _client, **kwargs):
            self.requests.append(kwargs)
            tool_call = SimpleNamespace(
                function=SimpleNamespace(
                    name="emit_sidecar_verdict",
                    arguments='{"verdict":"revise","reason":"Add import"}',
                )
            )
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call])
            )])

    provider = Provider()
    observed = []
    verdict = invoke_sidecar_verifier(
        provider=provider,
        client=object(),
        model="judge-model",
        context=VerifierContext(("fix parser",), (), (), "done"),
        timeout_seconds=1,
        observer=lambda name, payload: observed.append((name, payload)),
    )

    request = provider.requests[0]
    assert request["model"] == "judge-model"
    assert request["max_tokens"] == 1024
    assert request["stream"] is False
    assert len(request["tools"]) == 1
    assert request["tools"][0]["function"]["name"] == "emit_sidecar_verdict"
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "emit_sidecar_verdict"},
    }
    assert [message["role"] for message in request["messages"]] == ["system", "user"]
    system_prompt = request["messages"][0]["content"]
    assert "exact local registration and validation pattern" in system_prompt
    assert "deleting existing persisted data" in system_prompt
    assert verdict.verdict == "revise"
    assert verdict.reason == "Add import"
    finish = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finish) == 1
    assert finish[0]["purpose"] == "verifier"


def test_deepseek_v4_verifier_disables_thinking_for_bounded_structured_output():
    """Prevent reasoning tokens from consuming the entire sidecar budget."""
    from types import SimpleNamespace

    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierContext,
        invoke_sidecar_verifier,
    )

    class Provider:
        name = "openai-compatible"

        def __init__(self):
            self.requests = []

        def create_completion(self, _client, **kwargs):
            self.requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Evidence is complete"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    reasoning_content="",
                    tool_calls=[call],
                ),
                finish_reason="tool_calls",
            )])

    provider = Provider()
    verdict = invoke_sidecar_verifier(
        provider=provider,
        client=object(),
        model="deepseek-v4-flash",
        context=VerifierContext(("preserve compatibility",), (), (), "done"),
        timeout_seconds=1,
    )

    assert verdict.trace == "verifier_ok"
    assert provider.requests[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
    }


def test_provider_adapter_accepts_strict_json_compatibility_response():
    """Catches Providers without normalized tool blocks losing valid judgement."""
    from types import SimpleNamespace

    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierContext,
        invoke_sidecar_verifier,
    )

    class Provider:
        name = "json-only"

        def create_completion(self, _client, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"verdict":"blocked","reason":"Need permission"}',
                tool_calls=[],
            ))])

    verdict = invoke_sidecar_verifier(
        provider=Provider(),
        client=object(),
        model="judge-model",
        context=VerifierContext(("modify repo",), (), (), "cannot"),
        timeout_seconds=1,
    )

    assert verdict.verdict == "blocked"
    assert verdict.reason == "Need permission"


def test_verifier_provider_resolution_requires_a_complete_valid_override():
    """Catches partial or invalid env overrides replacing the Main Provider."""
    from nz_coder.runtime.verification.sidecar_verifier import resolve_verifier_provider

    class Provider:
        def __init__(self, name):
            self.name = name
            self.create_client_calls = 0

        def create_client(self):
            self.create_client_calls += 1
            return f"{self.name}-client"

    main = Provider("main")
    explicit = Provider("explicit")

    def factory(name):
        return explicit if name == "explicit" else None

    inherited = resolve_verifier_provider(
        main_provider=main,
        main_client="main-client",
        main_model="main-model",
        env={"KODAX_VERIFIER_PROVIDER": "explicit"},
        provider_factory=factory,
    )
    invalid = resolve_verifier_provider(
        main_provider=main,
        main_client="main-client",
        main_model="main-model",
        env={
            "KODAX_VERIFIER_PROVIDER": "typo",
            "KODAX_VERIFIER_MODEL": "judge-model",
        },
        provider_factory=factory,
    )
    overridden = resolve_verifier_provider(
        main_provider=main,
        main_client="main-client",
        main_model="main-model",
        env={
            "KODAX_VERIFIER_PROVIDER": "explicit",
            "KODAX_VERIFIER_MODEL": "judge-model",
        },
        provider_factory=factory,
    )

    assert (inherited.provider_name, inherited.model, inherited.source) == (
        "main", "main-model", "inherit-main"
    )
    assert invalid.source == "inherit-main"
    assert overridden.provider is explicit
    assert overridden.client == "explicit-client"
    assert overridden.model == "judge-model"
    assert overridden.source == "explicit-env"
    assert overridden.owns_client is True
    assert explicit.create_client_calls == 1


def test_semantic_contract_forces_sidecar_with_task_and_real_diff_evidence():
    """Compatibility review must not be skipped as a trivial one-file edit."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Add named ranges while preserving numeric inputs",
        "requirements": [{
            "id": "R1",
            "description": "Preserve numeric and Sunday compatibility",
            "kind": "compatibility",
        }],
        "acceptance_commands": ["pytest -q tests"],
        "contract_version": 2,
    })
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["parser.py"])
    ledger.observe_verification(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    requests = []
    semantic_evidence = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments=(
                    '{"verdict":"accept","reason":"Compatibility evidence is present"}'
                ),
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    tracker = SimpleNamespace(
        current_changed_paths=lambda: ["parser.py"],
        current_deleted_paths=lambda: [],
        render_current_diff=lambda: (
            "diff --git a/parser.py b/parser.py\n"
            "--- a/parser.py\n+++ b/parser.py\n"
            + (" context line before compatibility branch\n" * 14)
            + "-if low > high: raise ValueError\n"
            "+if low > high and not named: raise ValueError\n"
        ),
    )
    runtime_state = SimpleNamespace(
        observe_requirement_semantic_review=lambda **kwargs: semantic_evidence.append(kwargs),
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=tracker,
            runtime_state=runtime_state,
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(Provider(), object(), "judge-model", "judge", "inherit-main"),
        env={},
    )
    context = StopHookContext(
        transcript=({"role": "user", "content": "Preserve numeric compatibility"},),
        last_assistant_text="Exact tests passed.",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 2,
            "mutation_generation": 2,
            "acceptance_mutation_generation": 1,
            "task_contract": contract.to_dict(),
            "requirement_ledger": ledger.to_dict(),
            "verification_contract": {
                "command": "pytest -q tests",
                "passed": True,
                "attempted_generation": 1,
                "output": "125 passed in 0.53s",
            },
        },
    )

    decision = asyncio.run(hook(context))

    assert decision.action == "complete"
    assert hook.stats["last_gate_reason"] == "semantic-contract"
    assert len(requests) == 1
    verifier_message = requests[0]["messages"][1]["content"]
    assert "Preserve numeric and Sunday compatibility" in verifier_message
    assert "Required evidence: semantic_review" in verifier_message
    assert "Trusted exact-acceptance output: 125 passed in 0.53s" in verifier_message
    assert "-if low > high: raise ValueError" in verifier_message
    assert "+if low > high and not named: raise ValueError" in verifier_message
    assert semantic_evidence == [{
        "accepted": True,
        "fingerprint": "verifier_ok:compatibility",
    }]


def test_semantic_contract_prioritizes_compatibility_delta_evidence():
    """A compatibility review must see baseline-changing hunks, not doc prefixes."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Add named ranges while preserving the numeric API",
        "requirements": [
            {
                "id": "R1",
                "description": "Add named range syntax",
                "kind": "behavior",
                "expected_artifacts": ["parser.py"],
            },
            {
                "id": "R2",
                "description": "Preserve numeric range behavior",
                "kind": "compatibility",
            },
        ],
        "acceptance_commands": ["pytest -q tests"],
        "contract_version": 2,
    })
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(
        1,
        ["README.md", "parser.py", "tests/test_cli.py", "tests/test_parser.py"],
    )
    ledger.observe_verification(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments=(
                    '{"verdict":"revise","reason":"numeric behavior changed"}'
                ),
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    noisy_docs = "+" + ("documentation filler " * 180)
    noisy_parser_prefix = "+" + ("source filler " * 220)
    noisy_cli_tests = "+" + ("def test_unrelated_cli(): assert True\n+" * 220)
    tracker = SimpleNamespace(
        current_changed_paths=lambda: [
            "README.md",
            "parser.py",
            "tests/test_cli.py",
            "tests/test_parser.py",
        ],
        current_deleted_paths=lambda: [],
        render_current_diff=lambda: (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n"
            f"{noisy_docs}\n"
            "diff --git a/parser.py b/parser.py\n"
            "--- a/parser.py\n+++ b/parser.py\n@@ -1 +1,2 @@\n"
            f"{noisy_parser_prefix}\n"
            "@@ -90,3 +91,5 @@\n"
            "-if low > high: raise ValueError('descending range')\n"
            "+if low > high and not allow_wrap: raise ValueError('descending range')\n"
            "+allow_wrap = field_name == 'day_of_week'\n"
            "diff --git a/tests/test_cli.py b/tests/test_cli.py\n"
            "--- a/tests/test_cli.py\n+++ b/tests/test_cli.py\n@@ -1 +1,220 @@\n"
            f"{noisy_cli_tests}\n"
            "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
            "--- a/tests/test_parser.py\n+++ b/tests/test_parser.py\n@@ -1 +1,3 @@\n"
            "+def test_cross_week_numeric_range():\n"
            "+    assert parse('5-1').values == [0, 1, 5, 6]\n"
        ),
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=tracker,
            runtime_state=SimpleNamespace(
                observe_requirement_semantic_review=lambda **_kwargs: None,
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main",
        ),
        env={},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=({
            "role": "user",
            "content": "Add named ranges while preserving the numeric API",
        },),
        last_assistant_text="All tests pass.",
        runtime_state={
            "edits_this_run": 3,
            "turn_count": 8,
            "mutation_generation": 1,
            "task_contract": contract.to_dict(),
            "requirement_ledger": ledger.to_dict(),
            "verification_contract": {
                "command": "pytest -q tests",
                "passed": True,
                "attempted_generation": 1,
            },
        },
    )))

    assert decision.action == "reanimate"
    verifier_message = requests[0]["messages"][1]["content"]
    assert "SEMANTIC CONTRACT CERTIFICATION MODE" in verifier_message
    assert "COMPATIBILITY DELTA EVIDENCE" in verifier_message
    delta_section = verifier_message.split("COMPATIBILITY DELTA EVIDENCE", 1)[1]
    assert "-if low > high: raise ValueError" in delta_section
    assert "+allow_wrap = field_name == 'day_of_week'" in delta_section
    assert "+def test_cross_week_numeric_range" in delta_section


def test_semantic_contract_rejects_broad_validation_relaxation_after_model_accept():
    """A field-wide bypass cannot certify preservation of legacy inputs."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Add named ranges while preserving numeric ranges",
        "requirements": [{
            "id": "R1",
            "description": "Preserve numeric range behavior",
            "kind": "compatibility",
            "expected_artifacts": ["parser.py"],
        }],
        "acceptance_commands": ["pytest -q tests"],
        "contract_version": 2,
    })
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["parser.py"])
    ledger.observe_verification(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    recorded = []
    provider_calls = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **_kwargs):
            provider_calls.append(_kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments=(
                    '{"verdict":"accept","reason":"numeric behavior is preserved"}'
                ),
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -10,3 +10,8 @@\n"
        "-if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "+wrapped = lo > hi\n"
        "+if wrapped and not allow_wrap:\n"
        "+    raise ValueError('descending range')\n"
        "+if wrapped:\n"
        "+    values = expand_wrap(lo, hi)\n"
        "@@ -40 +45,4 @@\n"
        "-values = expand(token, field_name)\n"
        "+values = expand(\n"
        "+    token, field_name,\n"
        "+    allow_wrap=(field_name == 'day_of_week'),\n"
        "+)\n"
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["parser.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: diff,
            ),
            runtime_state=SimpleNamespace(
                observe_requirement_semantic_review=lambda **kwargs: recorded.append(
                    kwargs
                ),
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main",
        ),
        env={},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=({
            "role": "user",
            "content": "Add named ranges while preserving numeric ranges",
        },),
        last_assistant_text="Done",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 4,
            "mutation_generation": 1,
            "task_contract": contract.to_dict(),
            "requirement_ledger": ledger.to_dict(),
            "verification_contract": {
                "command": "pytest -q tests",
                "passed": True,
                "attempted_generation": 1,
            },
        },
    )))

    assert decision.action == "reanimate"
    assert "broad compatibility relaxation" in decision.message
    assert "allow_wrap" in decision.message
    assert recorded == []
    assert provider_calls == []


def test_compatibility_guard_allows_new_syntax_specific_relaxation():
    """A legacy guard may be relaxed only when the new token syntax gates it."""
    from nz_coder.runtime.verification.sidecar_verifier import _broad_compatibility_relaxation

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -10,3 +10,7 @@\n"
        "-if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "+wrap_range = lo > hi and lo_is_name and hi_is_name\n"
        "+if lo > hi and not wrap_range:\n"
        "+    raise ValueError('descending range')\n"
        "+if wrap_range:\n"
        "+    values = expand_wrap(lo, hi)\n"
    )

    assert _broad_compatibility_relaxation(diff, ["parser.py"]) == ""


def test_compatibility_guard_rejects_nested_field_wide_relaxation():
    """Keeping the outer guard as context must not hide a new broad bypass."""
    from nz_coder.runtime.verification.sidecar_verifier import _broad_compatibility_relaxation

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,5 +20,10 @@\n"
        " if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "+    if not allow_wrap:\n"
        "+        raise ValueError('descending range')\n"
        "+    span = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        " else:\n"
        "     span = range(lo, hi + 1)\n"
        "@@ -50,3 +55,6 @@\n"
        "-values = expand(token, field_name)\n"
        "+values = expand(\n"
        "+    token, field_name,\n"
        "+    allow_wrap=(field_name == 'day_of_week'),\n"
        "+)\n"
    )

    risk = _broad_compatibility_relaxation(diff, ["parser.py"])

    assert "broad compatibility relaxation" in risk
    assert "allow_wrap" in risk


def test_compatibility_guard_allows_nested_new_syntax_gate():
    """A nested bypass is safe when its condition proves both atoms are names."""
    from nz_coder.runtime.verification.sidecar_verifier import _broad_compatibility_relaxation

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,3 +20,6 @@\n"
        " if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "+    if not (lo_is_name and hi_is_name):\n"
        "+        raise ValueError('descending range')\n"
        "+    span = expand_wrap(lo, hi)\n"
    )

    assert _broad_compatibility_relaxation(diff, ["parser.py"]) == ""


def test_compatibility_guard_rejects_positive_field_gate_with_legacy_else():
    """A field identity is not proof that a descending token uses new syntax."""
    from nz_coder.runtime.verification.sidecar_verifier import _broad_compatibility_relaxation

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,3 +20,9 @@\n"
        "-if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "+if lo > hi:\n"
        "+    if min_val == 0 and max_val == 7:\n"
        "+        seq = expand_wrap(lo, hi)\n"
        "+    else:\n"
        "+        raise ValueError('descending range')\n"
    )

    risk = _broad_compatibility_relaxation(diff, ["parser.py"])

    assert "broad compatibility relaxation" in risk
    assert "min_val == 0 and max_val == 7" in risk


def test_compatibility_guard_rejects_conjoined_field_wrap_gate():
    """A field-level wrap flag must not make every numeric descending range valid."""
    from nz_coder.runtime.verification.sidecar_verifier import _broad_compatibility_relaxation

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,8 +20,14 @@\n"
        "+if allow_wrap and lo > hi:\n"
        "+    seq = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+else:\n"
        "+    if lo > hi:\n"
        "+        raise ValueError('descending range')\n"
        "-if lo > hi:\n"
        "-    raise ValueError('descending range')\n"
        "-for value in range(lo, hi + 1):\n"
        "+for value in seq:\n"
        "     values.add(value)\n"
    )

    risk = _broad_compatibility_relaxation(diff, ["parser.py"])

    assert "broad compatibility relaxation" in risk
    assert "allow_wrap and lo > hi" in risk
    assert "input syntax" in risk


def test_semantic_guard_rejects_value_delta_step_on_wrapped_sequence():
    """A wrapped range must step by traversal position, not reset numeric value."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,6 +20,11 @@\n"
        "+if wrap_range:\n"
        "+    span = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+else:\n"
        "+    span = range(lo, hi + 1)\n"
        " for v in span:\n"
        "     if step is None or (v - lo) % step == 0:\n"
        "         values.add(v)\n"
    )

    risk = _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    )

    assert "wrapped range step" in risk
    assert "traversal position" in risk


def test_semantic_guard_rejects_segmented_wrap_with_value_delta_step():
    """Two wrap segments cannot both reuse a raw value delta as position."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,3 +20,13 @@\n"
        "+if wrap_range:\n"
        "+    for value in range(lo, max_val + 1):\n"
        "+        if step is None or (value - lo) % step == 0:\n"
        "+            values.add(value)\n"
        "+    for value in range(min_val, hi + 1):\n"
        "+        if step is None or (value - lo) % step == 0:\n"
        "+            values.add(value)\n"
    )

    risk = _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    )

    assert "segmented wrapped range step" in risk
    assert "continuous traversal index" in risk
    assert "Sunday exactly once" in risk


def test_semantic_guard_allows_index_step_on_wrapped_sequence():
    """Index-based filtering preserves step semantics across the wrap point."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,3 +20,8 @@\n"
        "+span = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+for index, value in enumerate(span):\n"
        "+    if step is None or index % step == 0:\n"
        "+        values.add(value)\n"
    )

    assert _wrapped_sequence_step_risk(diff, ["parser.py"]) == ""


def test_semantic_guard_rejects_alias_normalization_after_wrapped_step():
    """Equivalent endpoints cannot both occupy positions before a range stride."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,5 +20,12 @@\n"
        "+seq = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+if step is not None:\n"
        "+    seq = seq[::step]\n"
        "+values.update(seq)\n"
        " if 7 in values:\n"
        "     values.discard(7)\n"
        "     values.add(0)\n"
    )

    risk = _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    )

    assert "alias endpoints" in risk
    assert "before applying the step" in risk


def test_semantic_guard_rejects_index_step_before_alias_deduplication():
    """Index stepping is still wrong when 0 and 7 both occupy the ring."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,5 +20,13 @@\n"
        "+seq = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+for index, value in enumerate(seq):\n"
        "+    if step is None or index % step == 0:\n"
        "+        values.add(value)\n"
        " if 7 in values:\n"
        "     values.discard(7)\n"
        "     values.add(0)\n"
    )

    risk = _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    )

    assert "alias endpoints" in risk
    assert "before applying the step" in risk


def test_semantic_guard_allows_order_preserving_alias_dedup_before_index_step():
    """Canonicalizing 7 to 0 before enumerate leaves one Sunday position."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,5 +20,13 @@\n"
        "+seq = list(range(lo, max_val + 1)) + list(range(min_val, hi + 1))\n"
        "+seq = list(dict.fromkeys(0 if v == 7 else v for v in seq))\n"
        "+for index, value in enumerate(seq):\n"
        "+    if step is None or index % step == 0:\n"
        "+        values.add(value)\n"
    )

    assert _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    ) == ""


def test_deterministic_risks_are_merged_with_model_revision():
    """One reanimation must expose static and model findings together."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VerifierVerdict,
        _merge_compatibility_risk,
    )

    merged = _merge_compatibility_risk(
        VerifierVerdict(
            "revise",
            "Add a missing boundary test.",
            trace="verifier_ok",
        ),
        "Detected broad compatibility relaxation in parser.py.",
    )

    assert merged.verdict == "revise"
    assert merged.trace == "deterministic_compatibility_guard"
    assert "broad compatibility relaxation" in merged.reason
    assert "missing boundary test" in merged.reason


def test_semantic_guard_allows_canonical_wrap_before_step_with_alias_endpoints():
    """Excluding the numeric alias endpoint makes index-based stepping sound."""
    from nz_coder.runtime.verification.sidecar_verifier import _wrapped_sequence_step_risk

    diff = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n+++ b/parser.py\n@@ -20,5 +20,11 @@\n"
        "+seq = list(range(lo, max_val)) + list(range(min_val, hi + 1))\n"
        "+if step is not None:\n"
        "+    seq = seq[::step]\n"
        "+values.update(seq)\n"
        " if 7 in values:\n"
        "     values.discard(7)\n"
        "     values.add(0)\n"
    )

    assert _wrapped_sequence_step_risk(
        diff,
        ["parser.py"],
        compatibility_context="Preserve 0/7 Sunday alias semantics",
    ) == ""


def test_fail_open_accept_does_not_create_semantic_evidence():
    """A missing verdict may unblock ordinary stops but cannot prove compatibility."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve compatibility",
            "kind": "compatibility",
        }],
    })
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["a.py"])
    ledger.observe_verification(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    recorded = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="no structured verdict", tool_calls=[])
            )])

    loop = SimpleNamespace(
        change_tracker=SimpleNamespace(
            current_changed_paths=lambda: ["a.py"],
            current_deleted_paths=lambda: [],
            render_current_diff=lambda: "--- a/a.py\n+++ b/a.py\n-old\n+new\n",
        ),
        runtime_state=SimpleNamespace(
            observe_requirement_semantic_review=lambda **kwargs: recorded.append(kwargs),
        ),
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
        _sidecar_risky_shell_ops=0,
        _sidecar_unattributed_write_ops=0,
    )
    hook = create_sidecar_verifier_hook(
        loop,
        ResolvedVerifierProvider(Provider(), object(), "judge-model", "judge", "inherit-main"),
        env={},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=({"role": "user", "content": "Preserve compatibility"},),
        last_assistant_text="Done",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 2,
            "mutation_generation": 1,
            "task_contract": contract.to_dict(),
            "requirement_ledger": ledger.to_dict(),
        },
    )))

    assert decision.action == "complete"
    assert hook.stats["last_trace"] == "no_tool_call"
    assert recorded == []


def test_sidecar_hook_builds_live_evidence_traces_and_maps_verdict():
    """Catches a resolved verifier that is never assembled into a live stop hook."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    class Provider:
        name = "judge"

        def create_completion(self, _client, **_kwargs):
            tool_call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments=(
                    '{"verdict":"revise","reason":"Missing test",'
                    '"suggestedFix":"Add regression coverage"}'
                ),
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call])
            )])

    class Tracker:
        def current_changed_paths(self):
            return ["a.py", "b.py"]

        def current_deleted_paths(self):
            return []

        def render_current_diff(self):
            return "--- a/a.py\n+++ b/a.py\n-old\n+new\n--- a/b.py\n+++ b/b.py\n+x\n"

    class Tracer:
        def __init__(self):
            self.events = []

        def log(self, event, **payload):
            self.events.append((event, payload))

    tracer = Tracer()
    loop = SimpleNamespace(
        change_tracker=Tracker(),
        tracer=tracer,
        _sidecar_risky_shell_ops=0,
        _sidecar_unattributed_write_ops=0,
    )
    hook = create_sidecar_verifier_hook(
        loop,
        ResolvedVerifierProvider(
            provider=Provider(),
            client=object(),
            model="judge-model",
            provider_name="judge",
            source="inherit-main",
        ),
        env={},
    )
    context = StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix both modules"},
            {"role": "assistant", "content": "Done"},
        ),
        last_assistant_text="Done",
        runtime_state={"edits_this_run": 2, "turn_count": 4},
    )

    decision = asyncio.run(hook(context))

    assert decision.action == "reanimate"
    assert "Missing test" in decision.message
    assert hook.stats["fire_count"] == 1
    assert hook.stats["verdict_counts"]["revise"] == 1
    assert [event for event, _payload in tracer.events] == [
        "sidecar_gate_decision",
        "sidecar_started",
        "sidecar_finished",
    ]
    assert tracer.events[-1][1]["provider"] == "judge"
    assert tracer.events[-1][1]["model"] == "judge-model"


def test_sidecar_reuses_accepted_verdict_for_unchanged_mutation_evidence():
    """An accepted patch generation must not pay for the same semantic review again."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            tool_call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Patch is sound"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call])
            )])

    events = []
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["pkg/parser.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: (
                    "--- a/pkg/parser.py\n+++ b/pkg/parser.py\n-old\n+new\n"
                ),
            ),
            tracer=SimpleNamespace(
                log=lambda name, **payload: events.append((name, payload))
            ),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )

    def context(generation, final_text):
        return StopHookContext(
            transcript=(
                {"role": "user", "content": "Fix parser aliases"},
                {"role": "assistant", "content": final_text},
            ),
            last_assistant_text=final_text,
            runtime_state={
                "edits_this_run": generation,
                "turn_count": 12,
                "mutation_generation": generation,
            },
        )

    first = asyncio.run(hook(context(1, "Implemented and verified.")))
    cached = asyncio.run(hook(context(1, "Here is a shorter final summary.")))
    changed = asyncio.run(hook(context(2, "Adjusted the implementation.")))

    assert first.action == cached.action == changed.action == "complete"
    assert len(requests) == 2
    assert hook.stats["fire_count"] == 2
    assert hook.stats["skip_count"] == 1
    assert any(
        name == "sidecar_gate_decision"
        and payload.get("reason") == "accepted-evidence-cache"
        and payload.get("fire") is False
        for name, payload in events
    )


def test_sidecar_prompt_preserves_complete_medium_single_file_diff():
    """A normal source patch must not be hidden behind a tiny diff preview."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Patch is complete"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    body = "".join(
        f"+    normalized_{index} = values[{index % 7}]\n"
        for index in range(150)
    )
    diff = (
        "diff --git a/pkg/visitor.py b/pkg/visitor.py\n"
        "--- a/pkg/visitor.py\n"
        "+++ b/pkg/visitor.py\n"
        "@@ -10,2 +10,153 @@\n"
        + body
        + "+    return COMPLETE_METHOD_SENTINEL\n"
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["pkg/visitor.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: diff,
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=({"role": "user", "content": "Complete the visitor method"},),
        last_assistant_text="Implemented and verified.",
        runtime_state={"edits_this_run": 1, "turn_count": 6},
    )))

    assert decision.action == "complete"
    prompt = requests[0]["messages"][-1]["content"]
    assert "COMPLETE_METHOD_SENTINEL" in prompt
    assert "[diff truncated]" not in prompt


def test_sidecar_diff_hints_include_truncation_markers_inside_hard_budget():
    """Per-file truncation metadata must not leak beyond the total diff cap."""
    from nz_coder.runtime.verification.sidecar_verifier import (
        VERIFIER_DIFF_MAX_EACH,
        VERIFIER_DIFF_MAX_TOTAL,
        _bounded_diff_hints,
    )

    first = "+first = 1\n" * 1200
    second = "+second = 2\n" * 1200
    diff = (
        "diff --git a/pkg/first.py b/pkg/first.py\n"
        "--- a/pkg/first.py\n+++ b/pkg/first.py\n"
        + first
        + "diff --git a/pkg/second.py b/pkg/second.py\n"
        "--- a/pkg/second.py\n+++ b/pkg/second.py\n"
        + second
    )

    hints = _bounded_diff_hints(
        diff,
        ["pkg/first.py", "pkg/second.py"],
    )

    assert sum(len(value) for value in hints.values()) <= VERIFIER_DIFF_MAX_TOTAL
    assert all(len(value) <= VERIFIER_DIFF_MAX_EACH for value in hints.values())
    assert all("[diff truncated]" in value for value in hints.values())


def test_sidecar_receives_persisted_current_round_instruction(tmp_path):
    """Semantic completion review must retain the latest substantive follow-up."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Complete"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    state = RuntimeState(initial_task_text="Fix parser aliases")
    state.apply_current_round_instruction(
        "Continue and handle empty aliases.",
        workspace=tmp_path,
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: [],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: "",
            ),
            runtime_state=state,
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix parser aliases"},
            {"role": "assistant", "content": "Done"},
        ),
        last_assistant_text="Done",
        runtime_state=state.to_dict(active=True),
    )))

    assert decision.action == "complete"
    prompt = requests[0]["messages"][-1]["content"]
    assert (
        "Current round instruction: Continue and handle empty aliases."
        in prompt
    )


def test_sidecar_hook_skips_trivial_observed_work_without_provider_call():
    """Catches the gate charging a verifier call for a one-line trivial edit."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    class Provider:
        name = "judge"

        def create_completion(self, _client, **_kwargs):
            raise AssertionError("trivial work must not call the verifier")

    tracker = SimpleNamespace(
        current_changed_paths=lambda: ["a.py"],
        current_deleted_paths=lambda: [],
        render_current_diff=lambda: "--- a/a.py\n+++ b/a.py\n-old\n+new\n",
    )
    tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=tracker,
            tracer=tracer,
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(Provider(), object(), "judge-model", "judge", "inherit-main"),
        env={},
    )

    decision = asyncio.run(hook(StopHookContext(
        transcript=({"role": "user", "content": "Fix typo"},),
        last_assistant_text="Done",
        runtime_state={"edits_this_run": 1, "turn_count": 2},
    )))

    assert decision.action == "complete"
    assert hook.stats["fire_count"] == 0
    assert hook.stats["skip_count"] == 1


def test_sidecar_defers_to_pending_strict_verification_without_provider_call():
    """Strict deterministic verification owns convergence before the judge."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    class Provider:
        name = "judge"

        def create_completion(self, _client, **_kwargs):
            raise AssertionError("pending strict verification must run first")

    events = []
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["pkg/parser.py", "tests/test_parser.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: (
                    "--- a/pkg/parser.py\n+++ b/pkg/parser.py\n"
                    "-old\n+new\n"
                ),
            ),
            tracer=SimpleNamespace(
                log=lambda name, **payload: events.append((name, payload))
            ),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )
    context = StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix parser"},
            {"role": "assistant", "content": "Done"},
        ),
        last_assistant_text="Done",
        runtime_state={
            "edits_this_run": 2,
            "turn_count": 4,
            "verification": {
                "verification_needed": True,
                "verification_state": "failed_repairable",
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = asyncio.run(hook(context))

    assert decision.action == "complete"
    assert hook.stats["fire_count"] == 0
    assert hook.stats["skip_count"] == 1
    assert hook.stats["last_gate_reason"] == "deterministic-verification-pending"
    assert [name for name, _payload in events] == ["sidecar_gate_decision"]


def test_sidecar_receives_strict_environment_blocker_authority(tmp_path):
    """The verifier must not prescribe operations forbidden to the main Agent."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Patch is coherent"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    target = tmp_path / "pylint" / "lint" / "pylinter.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def configure(group):\n"
        "    group._addoption(\n"
        "        \"-c\",\n"
        "        action=\"store\",\n"
        "    )\n"
        "    group.addoption(\n"
        "        \"-co\",\n"
        "        action=\"store_true\",\n"
        "    )\n",
        encoding="utf-8",
    )
    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            workdir=tmp_path,
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["pylint/lint/pylinter.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: (
                    "--- a/pylint/lint/pylinter.py\n"
                    "+++ b/pylint/lint/pylinter.py\n"
                    "@@ -5,6 +5,7 @@ def configure(group):\n"
                    "     group.addoption(\n"
                    "+        \"-co\",\n"
                ),
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={"KODAX_VERIFIER_ALWAYS": "1"},
    )
    context = StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix recursive ignore paths"},
            {"role": "assistant", "content": "Source patch is ready"},
        ),
        last_assistant_text="Source patch is ready",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 8,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "command": "python3 -m pytest tests/lint/unittest_lint.py -q",
                    "output": "ModuleNotFoundError: No module named 'astroid'",
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = asyncio.run(hook(context))

    assert decision.action == "complete"
    prompt = requests[0]["messages"][-1]["content"]
    assert "RUNTIME-OWNED STRICT OFFLINE BLOCKER" in prompt
    assert "Do not request package installation" in prompt
    assert "No module named 'astroid'" in prompt
    assert "NEARBY SOURCE CONTEXT" in prompt
    assert 'group._addoption(' in prompt
    assert 'group.addoption(' in prompt
    assert "Do not claim knowledge of a gold patch or external reference fix" in prompt
    assert "deleting existing persisted data" in prompt


def test_strict_environment_blocker_bypasses_trivial_sidecar_skip():
    """A settled strict blocker still needs semantic review for a tiny patch."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Patch is coherent"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: ["pkg/parser.py"],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: (
                    "--- a/pkg/parser.py\n+++ b/pkg/parser.py\n"
                    "-old = True\n+new = True\n"
                ),
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={},
    )
    context = StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix the parser"},
            {"role": "assistant", "content": "Source patch is ready"},
        ),
        last_assistant_text="Source patch is ready",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 8,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "command": "python3 -m pytest tests/test_parser.py -q",
                    "output": "ModuleNotFoundError: No module named 'dependency'",
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = asyncio.run(hook(context))

    assert decision.action == "complete"
    assert len(requests) == 1
    assert hook.stats["fire_count"] == 1
    assert hook.stats["last_gate_reason"] == "blocked-environment-semantic-review"


def test_persistent_data_deletion_risk_bypasses_trivial_sidecar_skip():
    """A review-level deletion signal must reach the semantic verifier."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider,
        create_sidecar_verifier_hook,
    )

    requests = []

    class Provider:
        name = "judge"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(
                name="emit_sidecar_verdict",
                arguments='{"verdict":"accept","reason":"Deletion is justified"}',
            ))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )])

    hook = create_sidecar_verifier_hook(
        SimpleNamespace(
            change_tracker=SimpleNamespace(
                current_changed_paths=lambda: [
                    "django/contrib/auth/migrations/0011_permissions.py"
                ],
                current_deleted_paths=lambda: [],
                render_current_diff=lambda: (
                    "--- a/django/contrib/auth/migrations/0011_permissions.py\n"
                    "+++ b/django/contrib/auth/migrations/0011_permissions.py\n"
                    "+    permissions.delete()\n"
                ),
            ),
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            _sidecar_risky_shell_ops=0,
            _sidecar_unattributed_write_ops=0,
        ),
        ResolvedVerifierProvider(
            Provider(), object(), "judge-model", "judge", "inherit-main"
        ),
        env={},
    )
    context = StopHookContext(
        transcript=(
            {"role": "user", "content": "Fix permission migration conflicts"},
            {"role": "assistant", "content": "Source patch is ready"},
        ),
        last_assistant_text="Source patch is ready",
        runtime_state={
            "edits_this_run": 1,
            "turn_count": 4,
            "patch_risk": {
                "risk_signals": [{
                    "category": "persistent_data_deletion",
                    "severity": "review",
                    "detail": "Added permissions.delete() in an auth migration",
                }],
            },
        },
    )

    decision = asyncio.run(hook(context))

    assert decision.action == "complete"
    assert len(requests) == 1
    assert hook.stats["fire_count"] == 1
    assert hook.stats["last_gate_reason"] == "data-integrity-risk"
