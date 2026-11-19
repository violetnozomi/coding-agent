"""发布模型任务判断的纯元数据提案；正式运行时负责来源、版本及落盘。"""
from __future__ import annotations

from nz_coder.protocol.public_error import PublicInputError, format_public_error
from nz_coder.runtime.agent.task_intent import COMPLEXITIES, TASK_KINDS, parse_task_assessment
from nz_coder.tools import ToolOutput, register


def update_task_assessment(task_kinds: list, complexity: str, reason: str, uncertainties: list,
                           expected_revision: int) -> str:
    try:
        if type(expected_revision) is not int or expected_revision < 0:
            raise PublicInputError("expected_revision must be a nonnegative integer")
        value = parse_task_assessment({"task_kinds": task_kinds, "complexity": complexity,
                                       "reason": reason, "uncertainties": uncertainties})
    except ValueError as exc:
        return format_public_error(exc)
    return ToolOutput("Task assessment proposed; awaiting runtime acceptance.",
                      metadata={"task_assessment_proposal": {**value, "expected_revision": expected_revision}})


register(
    name="update_task_assessment",
    description=("Publish your current task understanding when useful: multiple task kinds, semantic complexity, "
                 "reason and unknowns. Revise after new evidence. Use expected_revision from the current "
                 "task-assessment block. This is advice, not permission, verification evidence or a completed plan. "
                 "Simple answers need no assessment call; do not call just to fill labels."),
    parameters={"type": "object", "properties": {
        "task_kinds": {"type": "array", "items": {"type": "string", "enum": list(TASK_KINDS)}},
        "complexity": {"type": "string", "enum": list(COMPLEXITIES)},
        "reason": {"type": "string"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "expected_revision": {"type": "integer", "minimum": 0},
    }, "required": ["task_kinds", "complexity", "reason", "uncertainties", "expected_revision"],
        "additionalProperties": False},
    handler=update_task_assessment, execution="serial", side_effect="mutates-state", plan_mode_allowed=True,
)
