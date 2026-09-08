"""Three scripted tmp-fixture tasks for offline P2 wiring, never model scores."""
from __future__ import annotations

import json

import httpx


def response(body, number, descriptor):
    task = descriptor["task_id"]
    assert task in {"T02", "T03", "T05"}, "Unexpected fourth or unselected worker"
    usage = dict(prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60,
                 completion_tokens=20, total_tokens=120)
    verdict_call = body.get("tool_choice") == {"type": "function", "function": {"name": "emit_sidecar_verdict"}}
    if verdict_call:
        # Deliberately expensive *synthetic* sidecars: the real admission boundary
        # must stop before task four, with a positive but insufficient balance.
        usage.update(prompt_tokens=340000, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=340000,
                     total_tokens=340020)
        name, arguments = "emit_sidecar_verdict", dict(verdict="accept", reason="offline only")
    else:
        operations = {
            "T02": [("read_file", dict(path="worklog/reports.py")),
                    ("edit_file", dict(path="worklog/reports.py",
                        old_text='return Decimal(str(sum(float(entry.hours) for entry in entries)))',
                        new_text='return sum((entry.hours for entry in entries), Decimal(0))'))],
            "T03": [("read_file", dict(path="textkit/stats.py")),
                    ("edit_file", dict(path="textkit/stats.py", old_text='key=lambda item: -item[1]',
                                       new_text='key=lambda item: (-item[1], item[0])'))],
            "T05": [("read_file", dict(path="textkit/pipeline.py")), ("read_file", dict(path="textkit/cli.py")),
                    ("edit_file", dict(path="textkit/pipeline.py", old_text='transform(line) for line in lines',
                                       new_text='transform(line, lower=lower) for line in lines')),
                    ("edit_file", dict(path="textkit/cli.py", old_text='normalize_lines((stdin or sys.stdin).readlines())',
                                       new_text='normalize_lines((stdin or sys.stdin).readlines(), lower=args.lower)'))],
        }[task]
        if descriptor["scenario"] == "p2_functional" and task == "T02":
            operations[1][1]["new_text"] = operations[1][1]["old_text"] + "  # offline incomplete attempt"
        operations += [
            ("write_file", dict(path="tests/test_offline_probe.py", content=
                '"""Offline wiring marker, not independent acceptance."""\n'
                'def test_offline_wiring_marker():\n    assert 2 + 2 == 4\n')),
            ("bash", dict(command="python -m pytest -q tests")),
        ]
        name, arguments = operations[number - 1] if number <= len(operations) else (None, None)
    message = dict(role="assistant", content="Offline wiring complete; public tests passed.")
    if name:
        message.update(content="", tool_calls=[dict(id=f"offline-{task}-{number}", type="function",
            function=dict(name=name, arguments=json.dumps(arguments)))])
    payload = dict(id=f"offline-{task}-{number}", object="chat.completion", created=1, model=body["model"],
        choices=[dict(index=0, message=message, finish_reason="tool_calls" if name else "stop")], usage=usage)
    if descriptor["scenario"] == "p2_unknown" and task == "T03" and verdict_call:
        payload.pop("usage")
    return httpx.Response(200, json=payload)
