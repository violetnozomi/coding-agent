"""真实开发需求中的禁止对象必须准确，策略拒绝不能冒充用户回复。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "prompt",
    [
        "请正常写代码和执行测试，不修改共享依赖、不安装第三方包。",
        "请完成测试；不修改共享配置。",
        "编写测试。不修改共享依赖。",
        "请运行测试，不修改文档。",
        "执行测试文件后，不修改共享依赖。",
        "完成测试，不要改动共享依赖。",
        "完成测试，不得修改任何共享依赖。",
        "完成测试，不要修改任何共享依赖。",
    ],
)
def test_other_immutable_objects_do_not_freeze_test_files(prompt, tmp_path):
    from nz_coder.runtime.agent.task_policy import task_forbids_test_changes
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    assert task_forbids_test_changes(prompt) is False
    state = RuntimeState()
    state.set_acceptance_criteria_from_text(prompt, workspace=tmp_path)
    assert (
        state.task_constraint_action("write_file", {"path": "tests/test_app.py"})
        == "allow"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "测试文件保持不变。",
        "测试文件必须保持不变，其余代码可修改。",
        "测试文件不修改，修复源码。",
        "测试用例不要改动。",
        "不要修改测试文件。",
        "do not modify the tests",
        "测试文件一律保持不变，只修复源码。",
        "测试文件本次不修改，只修复源码。",
        "测试文件不要改动任何内容，只修复源码。",
        "测试文件应当保持不变，只修复源码。",
        "测试文件不要改动任何一行，只修复源码。",
        "测试文件，不要改动，只修复源码。",
        "测试用例，不得改动。",
        "现有测试，不修改。",
        "测试文件，不要改动任何一行，只修复源码。",
        "测试文件，一律保持不变，只修复源码。",
        "测试文件，请不要改动，只修复源码。",
        "现有测试，请不要改动。",
        "执行测试，不修改测试文件。",
    ],
)
def test_explicit_test_immutability_still_blocks_mutations(prompt):
    from nz_coder.runtime.agent.task_policy import task_forbids_test_changes

    assert task_forbids_test_changes(prompt) is True


@pytest.mark.parametrize(
    "source,permission_denied,output,expected_user_denials,blocked",
    [
        ("policy", True, "Denied: action rejected by policy", 0, False),
        ("", False, "Denied: immutable test artifact", 0, False),
        (
            "interaction_failure",
            True,
            "Permission approval interface is not available.",
            0,
            True,
        ),
        (
            "approval_reply",
            True,
            "Permission approval rejected; this action was not executed.",
            1,
            True,
        ),
    ],
)
def test_batch_feedback_uses_actual_permission_origin(
    source, permission_denied, output, expected_user_denials, blocked
):
    from nz_coder.runtime.core.tool_context import ToolProjectionContext
    from nz_coder.runtime.tool_runtime.result_projection import (
        ProductionToolResultProjector,
    )
    from nz_coder.runtime.verification.hooks import (
        ToolBatchContext,
        all_writes_denied_hook,
    )
    from nz_coder.tool_platform.execution import ToolExecutionResult

    context = ToolProjectionContext(
        signal_from_metadata=lambda _: None,
        record_result=lambda result: result.dispatch_failed,
        trace_result=lambda *args, **kwargs: None,

        after_result=lambda *args: None,
    )
    metadata = (
        {"permission_source": source} if source else {"guardrail": "task_constraint"}
    )
    if source == "interaction_failure":
        metadata["permission_reason"] = "unavailable"
    result = ToolExecutionResult(
        "write_file",
        {"path": "tests/test_app.py"},
        output,
        False,
        True,
        False,
        True,
        permission_denied=permission_denied,
        metadata=metadata,
    )
    messages = []
    state = ProductionToolResultProjector().consume(
        context,
        [
            (
                0,
                {
                    "id": "write-1",
                    "function": {"name": "write_file", "arguments": "{}"},
                },
                result,
            )
        ],
        messages,
    )
    all_writes_denied_hook(
        ToolBatchContext(
            loop=SimpleNamespace(),
            messages=messages,
            manual_compact=False,
            used_todo=False,
            on_text=None,
            write_total=state["write_total"],
            write_denied=state["write_denied"],
        )
    )
    assert state["write_denied"] == expected_user_denials
    assert state["blocked"] is blocked
    feedback = [m for m in messages if m.get("role") == "user"]
    assert bool(feedback) is bool(expected_user_denials)


def test_actual_child_can_write_and_test_when_only_shared_dependencies_are_immutable(
    tmp_path, monkeypatch
):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_subagent import (
        FakeClient,
        FakeResponse,
        FakeMessage,
        FakeToolCall,
        _native_child_messages,
    )

    fake = FakeClient(
        [
            FakeResponse(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            "write_files_batch",
                            {
                                "files": [
                                    {
                                        "path": "app.py",
                                        "content": "def add(a, b): return a + b\n",
                                    },
                                    {
                                        "path": "tests/test_app.py",
                                        "content": "import unittest\nfrom app import add\nclass AppTest(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2, 3), 5)\n",
                                    },
                                ]
                            },
                        )
                    ]
                )
            ),
            FakeResponse(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            "bash",
                            {"command": "python -m unittest discover -s tests -v"},
                            call_id="test-app",
                        )
                    ]
                )
            ),
            FakeResponse(
                FakeMessage("Implemented app.py and its unit test; unittest passed.")
            ),
        ]
    )
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
    with (
        scoped_workdir(tmp_path),
        subagent.scoped_parent_context(session_id="constraint-parent"),
    ):
        result = subagent.run_subagent(
            "请实现 app.py 和测试，正常写代码和执行测试，不修改共享依赖、不安装第三方包。",
            agent_type="general-purpose",
            target_paths=["app.py", "tests"],
        )
    canonical = result.metadata["child_result"]
    assert canonical["status"] in {"completed", "completed_unverified"}, result
    assert canonical["changed_files"] == ["app.py", "tests/test_app.py"]
    state = subagent._load_subagent_state(
        "constraint-parent", result.metadata["child_session_id"], tmp_path
    )
    messages = _native_child_messages(state, "constraint-parent", tmp_path)
    assert any(
        m.get("role") == "tool" and "OK" in m.get("content", "") for m in messages
    )
    assert not any("所有写操作均被用户拒绝" in str(m.get("content")) for m in messages)
