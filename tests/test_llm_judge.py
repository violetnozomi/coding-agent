"""Translated contracts for InfCodeX's domain-neutral LLM judge kernel."""
from __future__ import annotations

import threading


def test_edit_distance_and_fuzzy_match_choose_exact_then_nearest():
    """Catches report-tool typos being dropped or outranking an exact call."""
    from nz_coder.runtime.verification.llm_judge import edit_distance, find_fuzzy_tool_match

    typo = {"name": "emit_sidecar_verdct", "input": {"verdict": "revise"}}
    exact = {"name": "emit_sidecar_verdict", "input": {"verdict": "accept"}}

    assert edit_distance("verdict", "verdct") == 1
    assert find_fuzzy_tool_match([typo], "emit_sidecar_verdict") == (typo, False)
    assert find_fuzzy_tool_match([typo, exact], "emit_sidecar_verdict") == (exact, True)
    assert find_fuzzy_tool_match(
        [{"name": "unrelated_tool", "input": {}}],
        "emit_sidecar_verdict",
    ) is None


def _request():
    from nz_coder.runtime.verification.llm_judge import JudgeRequest

    return JudgeRequest(
        system_prompt="judge",
        user_message="evidence",
        report_tool={"name": "emit_sidecar_verdict", "parameters": {"type": "object"}},
        report_tool_name="emit_sidecar_verdict",
    )


def _default(reason: str) -> dict:
    return {"verdict": "accept", "trace": reason}


def test_llm_judge_parses_one_fuzzy_report_tool():
    """Catches successful structured responses falling into fail-open."""
    from nz_coder.runtime.verification.llm_judge import JudgeResponse, invoke_llm_judge

    result = invoke_llm_judge(
        request=_request(),
        invoke=lambda _request: JudgeResponse(tool_blocks=({
            "name": "emit_sidecar_verdct",
            "input": {"verdict": "revise", "reason": "missing import"},
        },)),
        parse_tool_call=lambda block, exact: {
            **block["input"],
            "exact": exact,
        },
        default_verdict=_default,
        timeout_seconds=0.2,
    )

    assert result == {
        "verdict": "revise",
        "reason": "missing import",
        "exact": False,
    }


def test_llm_judge_failure_modes_are_distinct_and_fail_open():
    """Catches provider/no-tool/parser failures escaping or sharing the wrong tag."""
    from nz_coder.runtime.verification.llm_judge import JudgeResponse, invoke_llm_judge

    provider_error = invoke_llm_judge(
        request=_request(),
        invoke=lambda _request: (_ for _ in ()).throw(RuntimeError("offline")),
        parse_tool_call=lambda block, exact: block,
        default_verdict=_default,
    )
    no_tool = invoke_llm_judge(
        request=_request(),
        invoke=lambda _request: JudgeResponse(tool_blocks=()),
        parse_tool_call=lambda block, exact: block,
        default_verdict=_default,
    )
    parse_failure = invoke_llm_judge(
        request=_request(),
        invoke=lambda _request: JudgeResponse(tool_blocks=({
            "name": "emit_sidecar_verdict",
            "input": {},
        },)),
        parse_tool_call=lambda _block, _exact: (_ for _ in ()).throw(ValueError("bad")),
        default_verdict=_default,
    )

    assert provider_error["trace"] == "provider_error"
    assert no_tool["trace"] == "no_tool_call"
    assert parse_failure["trace"] == "parse_failure"


def test_llm_judge_timeout_ignores_late_provider_result():
    """Catches a hung or late judge blocking the Main Agent."""
    from nz_coder.runtime.verification.llm_judge import JudgeResponse, invoke_llm_judge

    release = threading.Event()

    def invoke(_request):
        release.wait(timeout=2)
        return JudgeResponse(tool_blocks=({
            "name": "emit_sidecar_verdict",
            "input": {"verdict": "blocked"},
        },))

    result = invoke_llm_judge(
        request=_request(),
        invoke=invoke,
        parse_tool_call=lambda block, exact: block["input"],
        default_verdict=_default,
        timeout_seconds=0.02,
    )
    release.set()

    assert result == {"verdict": "accept", "trace": "timeout"}


def test_llm_judge_cancellation_fails_open_before_timeout():
    """Catches caller cancellation being ignored until the full judge deadline."""
    from nz_coder.runtime.verification.llm_judge import JudgeResponse, invoke_llm_judge

    release = threading.Event()
    cancelled = threading.Event()
    cancelled.set()

    result = invoke_llm_judge(
        request=_request(),
        invoke=lambda _request: release.wait(timeout=2) or JudgeResponse(),
        parse_tool_call=lambda block, exact: block,
        default_verdict=_default,
        timeout_seconds=1,
        cancel_event=cancelled,
    )
    release.set()

    assert result == {"verdict": "accept", "trace": "cancelled"}
