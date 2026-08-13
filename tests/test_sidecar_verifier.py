"""Source-translated contracts for the InfCodeX coding Sidecar Verifier."""
from __future__ import annotations


def test_verifier_context_keeps_real_query_and_bounds_recent_transcript():
    """Catches synthetic guidance replacing the ask or unbounded judge context."""
    from nz_coder.runtime.sidecar_verifier import build_verifier_context

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
    from nz_coder.runtime.sidecar_verifier import (
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


def test_gate_first_match_order_covers_substantial_and_trivial_work():
    """Catches metric branches firing in the wrong order or skipping risky work."""
    from nz_coder.runtime.sidecar_verifier import (
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
    from nz_coder.runtime.sidecar_verifier import (
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


def test_verdict_parser_degrades_invalid_or_reasonless_blocking_results():
    """Catches malformed verifier output blocking or reanimating the Main Agent."""
    from nz_coder.runtime.sidecar_verifier import parse_verifier_report

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
    from nz_coder.runtime.sidecar_verifier import (
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

    from nz_coder.runtime.sidecar_verifier import (
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
    verdict = invoke_sidecar_verifier(
        provider=provider,
        client=object(),
        model="judge-model",
        context=VerifierContext(("fix parser",), (), (), "done"),
        timeout_seconds=1,
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
    assert verdict.verdict == "revise"
    assert verdict.reason == "Add import"


def test_provider_adapter_accepts_strict_json_compatibility_response():
    """Catches Providers without normalized tool blocks losing valid judgement."""
    from types import SimpleNamespace

    from nz_coder.runtime.sidecar_verifier import (
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
    from nz_coder.runtime.sidecar_verifier import resolve_verifier_provider

    class Provider:
        def __init__(self, name):
            self.name = name

        def create_client(self):
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


def test_sidecar_hook_builds_live_evidence_traces_and_maps_verdict():
    """Catches a resolved verifier that is never assembled into a live stop hook."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.hooks import StopHookContext
    from nz_coder.runtime.sidecar_verifier import (
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


def test_sidecar_hook_skips_trivial_observed_work_without_provider_call():
    """Catches the gate charging a verifier call for a one-line trivial edit."""
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.hooks import StopHookContext
    from nz_coder.runtime.sidecar_verifier import (
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
