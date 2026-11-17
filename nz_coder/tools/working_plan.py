"""提出阶段路线更新；当前运行时负责归属、提交版本和持久化。"""
from __future__ import annotations

from nz_coder.runtime.agent.planning_runtime import parse_working_plan_update
from nz_coder.tools import ToolOutput, register


def update_working_plan(plan: list, expected_revision: int) -> str:
    payload = {"plan": plan, "expected_revision": expected_revision}
    try:
        parse_working_plan_update(payload)
    except ValueError as exc:
        return "Error: " + str(exc)
    return ToolOutput("Working plan proposed; awaiting runtime acceptance.",
                      metadata={"working_plan_proposal": payload})


register(
    name="update_working_plan",
    description=("Replace your advisory stage route when useful for multi-stage work or new evidence "
                 "changes the approach. Send the complete route and current expected_revision. "
                 "Update only when needed; simple tasks need no plan. This does not change user "
                 "requirements, verification, permissions or an accepted handoff, and does not mark "
                 "work complete. Keep discoveries and reasons in working memory, not another route copy."),
    parameters={"type": "object", "properties": {
        "plan": {"type": "array", "minItems": 1, "items": {
            "type": "object", "properties": {
                "title": {"type": "string"}, "target": {"type": "string"},
                "verification": {"type": "string"}},
            "required": ["title"], "additionalProperties": False}},
        "expected_revision": {"type": "integer", "minimum": 0}},
        "required": ["plan", "expected_revision"], "additionalProperties": False},
    handler=update_working_plan, execution="serial", side_effect="mutates-state", plan_mode_allowed=True,
)
