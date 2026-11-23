"""有明确的执行前拒绝时，收尾评审必须核对具体调用。"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from nz_coder.runtime.verification import sidecar_verifier as verifier


def _recorded():
    path = Path(__file__).parents[1] / "fixtures/delivery_followup/denied-venv-report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _context(record, candidate=None):
    return verifier.build_verifier_context(
        record["messages"], record["candidate"] if candidate is None else candidate, file_edits=[],
    )


def _judge(report, context):
    return verifier.parse_verifier_report({"input": report}, True, context=context)


@pytest.mark.parametrize("status", ["completed", "error"])
def test_surrogate_in_a_tool_input_or_output_can_reach_the_frozen_and_review_views(status):
    from nz_coder.runtime.verification.report_evidence import project_denied_commands

    record = _recorded()
    owner, tool = record["messages"]
    scope = {"session_id": owner["_nz_session_id"], "interaction_id": owner["_nz_interaction_run_id"]}
    assert project_denied_commands(record["messages"], **scope).entries[0].receipt_id == "denied-cbc5614db91c6a04604879a8"
    args = {"command": "账本-" + chr(0xDCFF)}
    owner["tool_calls"][0]["function"]["arguments"] = args
    state = owner["_nz_parts"][0]["state"]
    state.update(status=status, input=args)
    tool["content"] = "输出-" + chr(0xDCFF)
    if status == "completed":
        state.pop("error")
        state.update(metadata={"exit": 0}, output=tool["content"])
    denied = project_denied_commands(record["messages"], **scope)
    assert denied.total == (1 if status == "error" else 0)
    request = verifier.build_verifier_user_message(_context(record))
    request.encode("utf-8")
    assert "账本-" in request and "输出-" in request and r"\udcff" in request


@pytest.mark.parametrize(("field", "size", "expected"), [
    ("command", 150000, "unavailable"), ("workdir", 150000, "unavailable"),
    ("command", 1400, "accept"), ("reason", 4500, "accept"),
])
def test_denial_review_checks_what_command_identity_was_actually_shown(field, size, expected):
    record = _recorded()
    owner = record["messages"][0]
    state = owner["_nz_parts"][0]["state"]
    material = "x" * (size // 2) + "MIDDLE_OF_IDENTITY" + "y" * (size // 2)
    if field == "reason":
        state["error"]["message"] = material
    else:
        state["input"][field] = material
        owner["tool_calls"][0]["function"]["arguments"] = json.dumps(state["input"])
    ctx = _context(record, "Use ledger list to see the entries.")
    item, = ctx.denied_commands.entries
    assert item.truncated
    if field != "reason":
        assert ("MIDDLE_OF_IDENTITY" in verifier.build_verifier_user_message(ctx)) == (expected == "accept")
    check = {"status": "not_claimed", "candidate_excerpt": ""}
    assert _judge({**record["report"], "denied_command_claims": {item.receipt_id: check}}, ctx).verdict == expected


def test_recorded_accept_cannot_omit_the_actual_refused_venv_attempt():
    record = _recorded()
    context = _context(record)
    # 原评审在自由说明中识别到了拒绝，却把交付评审填为 reviewed、issues 为空。
    assert record["report"]["verdict"] == "accept"
    assert _judge(record["report"], context).verdict == "unavailable"
    request = verifier._verifier_request(context)
    check = request.report_tool["function"]["parameters"]
    receipt, = context.denied_commands.entries
    assert check["properties"]["denied_command_claims"]["required"] == [receipt.receipt_id]
    assert "denied_command_claims" in check["required"]
    assert receipt.receipt_id in request.user_message and receipt.command in request.user_message


def test_blocked_preserves_its_reason_and_retires_only_a_confirmed_contradiction():
    record = _recorded()
    context = _context(record)
    receipt, = context.denied_commands.entries
    reason = "The missing build dependency requires an external action."
    blocked = {**record["report"], "verdict": "blocked", "reason": reason}
    assert verifier.map_verdict_to_stop_decision(_judge(blocked, context)).source == "sidecar-verifier"
    blocked["denied_command_claims"] = {
        receipt.receipt_id: {"status": "claimed_as_executed", "candidate_excerpt": "装上后 `ledger --version`"}}
    verdict = _judge(blocked, context)
    decision = verifier.map_verdict_to_stop_decision(verdict)
    assert (verdict.verdict, verdict.trace) == ("blocked", "denied_command_claim")
    assert (decision.action, decision.source, decision.message) == ("abort", "denied-command-claim", reason)
    blocked["reason"] = reason + ' The withdrawn claim was “装上后 `ledger --version`”.'
    quoted = _judge(blocked, context)
    assert quoted.reason == blocked["reason"]
    public = verifier.map_verdict_to_stop_decision(quoted).message
    assert reason in public and "装上后" not in public


@pytest.mark.parametrize("decision", ["accept", "revise"])
def test_exact_denial_conflict_requests_a_text_correction_without_echoing_the_false_claim(decision):
    record = _recorded()
    context = _context(record)
    receipt, = context.denied_commands.entries
    excerpt = "全新 3.13 venv 中 `pip install -e .` 装上后 `ledger --version`、`ledger undo --help` 正常"
    report = {**record["report"], "verdict": decision, "denied_command_claims": {
        receipt.receipt_id: {"status": "claimed_as_executed", "candidate_excerpt": excerpt}}}
    verdict = _judge(report, context)
    assert (verdict.verdict, verdict.trace, verdict.conflicts) == ("revise", "denied_command_claim", ())
    stop = verifier.map_verdict_to_stop_decision(verdict)
    assert (stop.action, stop.source) == ("reanimate", "denied-command-claim")
    assert receipt.receipt_id in stop.message and excerpt not in stop.message


@pytest.mark.parametrize("checks", ["wrong_reference", "wrong_excerpt", "unknown", "false_not_claimed"])
def test_uncertain_or_unbound_denial_assessment_cannot_approve(checks):
    record = _recorded()
    context = _context(record)
    receipt, = context.denied_commands.entries
    reference = "another-invocation" if checks == "wrong_reference" else receipt.receipt_id
    assessment = {"status": "not_claimed", "candidate_excerpt": ""}
    if checks == "wrong_excerpt":
        assessment = {"status": "claimed_as_executed", "candidate_excerpt": "absent from the draft"}
    elif checks == "unknown":
        assessment["status"] = "unknown"
    elif checks == "false_not_claimed":
        assessment["candidate_excerpt"] = "删除现在可以撤销了"
    report = {**record["report"], "denied_command_claims": {reference: assessment}}
    assert _judge(report, context).verdict == "unavailable"


@pytest.mark.parametrize("same_command", [False, True], ids=["other-environment", "later-retry"])
def test_usage_and_other_successful_invocations_can_be_delivered(same_command):
    record = _recorded()
    original_owner = record["messages"][0]
    good = deepcopy(original_owner)
    good["_nz_message_id"] = "other-message"
    original_args = original_owner["_nz_parts"][0]["state"]["input"]
    args = dict(original_args) if same_command else {"command": ".other-venv/bin/ledger --version"}
    good["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
    part = good["_nz_parts"][0]
    part.update(id="other-part", message_id=good["_nz_message_id"])
    part["state"] = {"status": "completed", "input": args, "output": "ledger 0.1.0", "metadata": {"exit": 0}}
    record["messages"].append(good)
    text = "使用 ledger undo 可以撤销删除。另一个调用返回 ledger 0.1.0。"
    context = _context(record, text)
    receipt, = context.denied_commands.entries
    report = {**record["report"], "denied_command_claims": {
        receipt.receipt_id: {"status": "not_claimed", "candidate_excerpt": ""}}}
    assert _judge(report, context).verdict == "accept"
    assert "ledger 0.1.0" in verifier._verifier_request(context).user_message


def test_reviewing_four_receipts_cannot_clear_an_omitted_earlier_denial():
    record = _recorded()
    owner = record["messages"][0]
    for index in range(4):
        other = deepcopy(owner)
        other["_nz_message_id"] = f"message-{index}"
        part = other["_nz_parts"][0]
        part.update(id=f"part-{index}", message_id=other["_nz_message_id"])
        record["messages"].append(other)
    context = _context(record)
    assert context.denied_commands.total == 5 and len(context.denied_commands.entries) == 4
    checks = {entry.receipt_id: {"status": "not_claimed", "candidate_excerpt": ""}
              for entry in context.denied_commands.entries}
    assert _judge({**record["report"], "denied_command_claims": checks}, context).verdict == "unavailable"


def test_runner_revises_the_report_only_and_accepts_the_corrected_delivery(monkeypatch, tmp_path):
    from test_delivery_assessment_runner import accepted, run_probe

    false_claim = "I installed the package in .fresh-venv and checked its version."
    command = "python -m pip install --no-index ."
    monkeypatch.setenv("ALLOW_BASH_PACKAGE_INSTALLS", "false")

    def report(index, request, _environment):
        schema = request["tools"][0]["function"]["parameters"]
        reference, = schema["properties"]["denied_command_claims"]["required"]
        assert command in request["messages"][1]["content"]
        check = ({"status": "claimed_as_executed", "candidate_excerpt": false_claim}
                 if index == 1 else {"status": "not_claimed", "candidate_excerpt": ""})
        return {**accepted(), "denied_command_claims": {reference: check}}, "tool_calls"

    result, hook, provider, snapshot = run_probe(
        monkeypatch, tmp_path, reports=report, candidate=false_claim,
        task="Describe how to use the application.", initial_actions=[[("bash", {"command": command})]],
    )
    assert result.status.value == "completed"
    assert (provider.mains, provider.reviews) == (3, 2)
    assert hook.stats["verdict_counts"]["revise"] == 1 and snapshot.reviews[-1].accepted
    assert result.metadata["authored_content"] == "A revised answer."
    assert "/message" in result.final_text and false_claim not in result.final_text
