"""合法摘要响应样例；无效响应测试必须显式传入原始文本。"""
from __future__ import annotations

import json


def summary_document(goal: str = "Continue the current task", **overrides) -> dict:
    return {
        "goal": goal,
        "constraints": [],
        "progress": {"done": [], "in_progress": [], "blocked": []},
        "findings": [],
        "decisions": [],
        "verification": [],
        "next_steps": [],
        "relevant_files": [],
        **overrides,
    }


def summary_json(goal: str = "Continue the current task", **overrides) -> str:
    return json.dumps(summary_document(goal, **overrides), ensure_ascii=False)
