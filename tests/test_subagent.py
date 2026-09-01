"""Tests for subagent isolation and context handoff."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest


def _native_child_messages(state: dict, parent_session_id: str, workspace: Path):
    from nz_coder.runtime.session.model import SessionIdentity
    from nz_coder.runtime.session.store import LegacyJsonSessionStore

    worktree = state.get("worktree")
    path = (
        Path(worktree["path"])
        if isinstance(worktree, dict) and worktree.get("path")
        else workspace / str(state.get("worktree_rel") or ".")
    )
    session = asyncio.run(LegacyJsonSessionStore().load(
        SessionIdentity(state["session_id"], parent_session_id),
        path,
    ))
    assert session is not None
    return session.transcript


class FakeFunction:
    def __init__(self, name: str, arguments):
        self.name = name
        self.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)


class FakeToolCall:
    def __init__(self, name: str, arguments, call_id: str = "call_1"):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ],
        }


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = "tool_calls" if message.tool_calls else "stop"


class FakeResponse:
    def __init__(self, message, usage=None):
        self.choices = [FakeChoice(message)]
        self.usage = usage


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses):
        self.chat = FakeChat(FakeCompletions(responses))


class FakeParentTracer:
    enabled = True

    def __init__(self):
        self.events = []

    def log(self, event: str, **payload):
        self.events.append((event, payload))


def _tmp_workdir():
    from nz_coder.foundation import config

    old = config.WORKDIR
    tmp = Path(tempfile.mkdtemp())
    config.WORKDIR = tmp
    return old, tmp


def _restore_workdir(old, tmp):
    from nz_coder.foundation import config

    config.WORKDIR = old
    shutil.rmtree(str(tmp), ignore_errors=True)


def _extract_subagent_session_id(result: str) -> str:
    marker = "[Subagent session: "
    start = result.find(marker)
    assert start != -1
    start += len(marker)
    end = result.find("]", start)
    assert end != -1
    return result[start:end]


def test_subagent_exposes_expected_tool_tiers():
    from nz_coder.runtime.agent.subagent import _subagent_tools

    explore_names = {spec["function"]["name"] for spec in _subagent_tools("explore")}
    plan_names = {spec["function"]["name"] for spec in _subagent_tools("plan")}
    general_names = {spec["function"]["name"] for spec in _subagent_tools("general-purpose")}
    reflection_names = {spec["function"]["name"] for spec in _subagent_tools("reflection")}

    assert {"load_optional_tools", "read_symbol", "find_symbol_callers", "glob_search"} <= explore_names
    assert "smart_search" not in explore_names
    assert "write_file" not in explore_names
    assert "write_file" not in plan_names
    assert "write_file" not in reflection_names
    assert {"review_run_evidence", "diff_status", "analyze_impact"} <= reflection_names
    assert {"write_file", "edit_file", "verify_changed_files", "apply_patch"} <= general_names


def test_dynamic_write_tool_is_hidden_from_read_only_subagents():
    from nz_coder.runtime.agent.subagent import _subagent_tools
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )
    name = "_test_dynamic_write"

    try:
        register(name, "test", {"type": "object", "properties": {}}, lambda: "ok", execution="write")
        explore_names = {spec["function"]["name"] for spec in _subagent_tools("explore")}
        general_names = {spec["function"]["name"] for spec in _subagent_tools("general-purpose")}
        assert name not in explore_names
        assert name in general_names
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_SPECS[:] = [spec for spec in TOOL_SPECS if spec["function"]["name"] != name]


def test_read_only_subagent_filters_by_side_effect_not_scheduler_mode():
    """A serial plugin must not gain read-child authority by scheduler choice."""
    from nz_coder.runtime.agent.subagent import _subagent_tools
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )
    names = ("_test_child_serial_fs", "_test_child_serial_state")

    try:
        for name, effect in zip(names, ("mutates-fs", "mutates-state")):
            register(
                name,
                "test",
                {"type": "object", "properties": {}},
                lambda: "ok",
                execution="serial",
                side_effect=effect,
            )
        explore_names = {
            spec["function"]["name"] for spec in _subagent_tools("explore")
        }
        general_names = {
            spec["function"]["name"]
            for spec in _subagent_tools("general-purpose")
        }

        assert not set(names) & explore_names
        assert set(names) <= general_names
    finally:
        for name in names:
            TOOL_HANDLERS.pop(name, None)
            TOOL_EXECUTION_MODES.pop(name, None)
            TOOL_SIDE_EFFECTS.pop(name, None)
            TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] not in names
        ]


def test_subagent_injects_parent_context(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    scratchpad.clear()
    try:
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "runtime_state.json").write_text(
            json.dumps({
                "active": True,
                "turn_count": 3,
                "has_diff": True,
                "changed_files": ["django/utils/http.py"],
                "acceptance_criteria": ["tests/test_http.py::test_date"],
            }),
            encoding="utf-8",
        )
        scratchpad.update("finding", "timezone parsing points at django/utils/http.py")
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        result = subagent.run_subagent("explore timezone parsing")

        system = fake.chat.completions.requests[0]["messages"][0]["content"]
        assert "Parent agent context" in system
        assert "django/utils/http.py" in system
        assert "timezone parsing points" in system
        assert "[Subagent status: completed]" in result
    finally:
        scratchpad.clear()
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_enters_native_agent_facade(monkeypatch):
    """A child must build a native request instead of calling legacy Runner(host)."""
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    config.SUBAGENT_MAX_TURNS = 1
    entered = []
    original = ProductRunEnvironment.run

    async def recording_run(self, messages, **kwargs):
        entered.append(self.session_id)
        return await original(self, messages, **kwargs)

    try:
        monkeypatch.setattr(ProductRunEnvironment, "run", recording_run)
        monkeypatch.setattr(
            subagent,
            "OpenAI",
            lambda **_kwargs: FakeClient([FakeResponse(FakeMessage("done"))]),
        )

        result = subagent.run_subagent("inspect the repository", agent_type="explore")

        assert "[Subagent status: completed]" in result
        assert len(entered) == 1
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        _restore_workdir(old, tmp)


def test_explore_subagent_uses_explore_model(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    old_explore_model = config.SUBAGENT_EXPLORE_MODEL
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    config.SUBAGENT_EXPLORE_MODEL = "haiku-model"
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        subagent.run_subagent("scan repo", agent_type="explore")

        assert fake.chat.completions.requests[0]["model"] == "haiku-model"
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        config.SUBAGENT_EXPLORE_MODEL = old_explore_model
        _restore_workdir(old, tmp)


def test_subagent_semantic_tier_publishes_route_facts(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    old_fast = config.SUBAGENT_EXPLORE_MODEL
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    config.SUBAGENT_EXPLORE_MODEL = "fast-child-model"
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"), usage={
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
        })])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent(
            "scan repo",
            agent_type="explore",
            model_hint="fast",
        )

        route = result.metadata["child_result"]["route_facts"]
        assert fake.chat.completions.requests[0]["model"] == "fast-child-model"
        assert route["requested_tier"] == "fast"
        assert route["tier_outcome"] == "applied"
        assert route["model_source"] == "tier"
        assert route["final_model"] == "fast-child-model"
        assert route["iterations"] == 1
        assert route["input_tokens"] == 11
        assert route["output_tokens"] == 4
        assert route["duration_ms"] >= 0
        assert result.metadata["child_result"]["summary_kind"] == "excerpt"
        assert result.metadata["child_result"]["digest"] == "done"
    finally:
        config.SUBAGENT_EXPLORE_MODEL = old_fast
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_fast_tier_never_downgrades_write_child():
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old_fast = config.SUBAGENT_EXPLORE_MODEL
    config.SUBAGENT_EXPLORE_MODEL = "fast-child-model"
    try:
        model, route = subagent._resolve_subagent_route(
            "general-purpose",
            "fast",
        )
    finally:
        config.SUBAGENT_EXPLORE_MODEL = old_fast

    assert model != "fast-child-model"
    assert route["tier_outcome"] == "fast-write-ineligible"
    assert "read-only" in route["fallback_reason"]


def test_subagent_injects_and_returns_validated_evidence_refs(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        (tmp / "evidence.txt").write_text("trusted only after reading\n", encoding="utf-8")
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent(
            "inspect",
            evidence_refs=["file:evidence.txt", "finding:check parser"],
        )

        system = fake.chat.completions.requests[0]["messages"][0]["content"]
        assert "## Known Evidence" in system
        assert "trusted only after reading" in system
        assert "check parser" in system
        assert result.metadata["child_result"]["evidence_refs"] == [
            "file:evidence.txt",
            "finding:check parser",
        ]
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_hard_postcondition_changes_terminal_result(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("I will inspect next.")),
            FakeResponse(FakeMessage("Still short.")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent(
            "inspect",
            verification={
                "enforcement": "hard",
                "min_final_text_chars": 100,
                "reject_preparatory_final_text": True,
            },
        )

        canonical = result.metadata["child_result"]
        assert canonical["status"] == "verification_failed"
        assert canonical["verification"]["ok"] is False
        assert canonical["verification"]["reasons"] == [
            "final text was shorter than the required 100 characters"
        ]
        assert "[Child task verification failed]" in canonical["final_text"]
        assert "## Machine-checkable Postconditions" in (
            fake.chat.completions.requests[0]["messages"][0]["content"]
        )
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_gets_one_same_session_verification_repair(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("verification-repair-parent")
        fake = FakeClient([
            FakeResponse(FakeMessage("short")),
            FakeResponse(FakeMessage("A sufficiently detailed terminal report.")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent(
            "produce a complete report",
            verification={
                "enforcement": "hard",
                "min_final_text_chars": 20,
            },
        )

        canonical = result.metadata["child_result"]
        assert len(fake.chat.completions.requests) == 2
        assert canonical["status"] == "completed"
        assert canonical["verification"]["ok"] is True
        assert canonical["route_facts"]["iterations"] == 2
        state = subagent._load_subagent_state(
            "verification-repair-parent",
            canonical["session_id"],
            tmp,
        )
        assert "messages" not in state
        from nz_coder.runtime.session.model import SessionIdentity
        from nz_coder.runtime.session.store import LegacyJsonSessionStore

        session = asyncio.run(LegacyJsonSessionStore().load(
            SessionIdentity(
                canonical["session_id"],
                "verification-repair-parent",
            ),
            tmp / state["worktree_rel"],
        ))
        assert session is not None
        repairs = [
            item for item in session.transcript
            if item.get("_nz_verification_repair") is True
        ]
        assert len(repairs) == 1
        assert "only automatic verification repair" in repairs[0]["content"]
        assert state["child_result"] == canonical
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_verification_repair_can_satisfy_write_postcondition(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("No changes needed.")),
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
                "write_file",
                {"path": "app.py", "content": "VALUE = 2\n"},
            )])),
            FakeResponse(FakeMessage("Implemented and verified app.py.")),
        ])
        original_dispatch = subagent.dispatch

        def fake_dispatch(name, args):
            if name == "verify_changed_files":
                return "OK: changed files compile"
            return original_dispatch(name, args)

        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)
        monkeypatch.setattr(subagent, "dispatch", fake_dispatch)

        result = subagent.run_subagent(
            "update app.py",
            agent_type="general-purpose",
            target_paths=["app.py"],
            verification={
                "requires_mutation": True,
                "required_changed_paths": ["app.py"],
            },
        )

        canonical = result.metadata["child_result"]
        assert canonical["status"] == "completed"
        assert canonical["verification"]["ok"] is True
        assert canonical["changed_files"] == ["app.py"]
        assert canonical["route_facts"]["iterations"] == 3
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_publishes_canonical_structured_child_result(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY

    schema = {
        "type": "object",
        "required": ["finding"],
        "properties": {"finding": {"type": "string"}},
        "additionalProperties": False,
    }
    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("structured-parent")
        fake = FakeClient([FakeResponse(FakeMessage(
            'Complete. ```json\n{"finding":"parser bug"}\n```'
        ))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent("inspect parser", output_schema=schema)

        canonical = result.metadata[CHILD_RESULT_KEY]
        assert canonical["status"] == "completed"
        assert canonical["final_text"].startswith("Complete.")
        assert "[Subagent session:" not in canonical["final_text"]
        assert canonical["structured"] == {"finding": "parser bug"}
        assert result.metadata["child_status"] == "completed"
        assert "## Required Output Format" in (
            fake.chat.completions.requests[0]["messages"][0]["content"]
        )
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_structured_output_gets_exactly_one_no_tool_repair(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY
    from nz_coder.runtime.conversation.structured_output import (
        STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT,
    )

    schema = {
        "type": "object",
        "required": ["finding"],
        "properties": {"finding": {"type": "string"}},
        "additionalProperties": False,
    }
    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("structured-repair-parent")
        fake = FakeClient([
            FakeResponse(FakeMessage("prose only")),
            FakeResponse(FakeMessage('```json\n{"finding":"fixed"}\n```')),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent("inspect parser", output_schema=schema)

        assert len(fake.chat.completions.requests) == 2
        repair = fake.chat.completions.requests[1]
        assert repair["tools"] == []
        assert repair["messages"][0]["content"] == (
            STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
        )
        assert result.metadata[CHILD_RESULT_KEY]["structured"] == {
            "finding": "fixed"
        }
        session_id = result.metadata["child_session_id"]
        state = json.loads(
            subagent._subagent_session_path(
                "structured-repair-parent", session_id, tmp
            ).read_text(encoding="utf-8")
        )
        messages = _native_child_messages(
            state, "structured-repair-parent", tmp,
        )
        repair_messages = [
            item for item in messages
            if item.get("_nz_structured_output_repair") is True
        ]
        assert len(repair_messages) == 1
        assert len([
            item for item in messages
            if item.get("role") == "assistant"
        ]) == 2
        assert state["structured_output_evaluation"]["ok"] is True
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_progress_uses_parent_task_metadata_channel():
    from nz_coder.runtime.agent import subagent
    from nz_coder.tools import scoped_tool_metadata_reporter

    updates = []
    with scoped_tool_metadata_reporter(
        lambda title, metadata: updates.append((title, metadata))
    ):
        assert subagent._report_subagent_progress(
            {"session_id": "child-1", "agent_type": "general-purpose"},
            status="running",
            description="  inspect   parser  ",
            current_tool="grep_search",
            current_title="find parser references",
            tool_count=2,
        ) is True

    assert updates == [(
        "General Purpose Task — inspect parser",
        {
            "child_session_id": "child-1",
            "child_status": "running",
            "child_tool_count": 2,
            "child_current_tool": "grep_search",
            "child_current_title": "find parser references",
        },
    )]


def test_public_subagent_session_reader_requires_exact_owned_id(tmp_path):
    from nz_coder.runtime.agent import subagent

    state = subagent._new_subagent_state("parent-1", "explore", None)
    state["session_id"] = "subagent-owned"
    state["status"] = "completed"
    state["model_id"] = "provider/model"
    state["messages"] = [{"role": "user", "content": "inspect"}]
    subagent._save_subagent_state("parent-1", state, tmp_path)

    summaries = subagent.list_subagent_sessions("parent-1", tmp_path)

    assert summaries[0]["session_id"] == "subagent-owned"
    assert summaries[0]["message_count"] == 1
    loaded = subagent.load_subagent_session("parent-1", "subagent-owned", tmp_path)
    loaded["messages"].clear()
    assert subagent.load_subagent_session(
        "parent-1", "subagent-owned", tmp_path,
    )["messages"]
    assert subagent.load_subagent_session("parent-1", "../subagent-owned", tmp_path) == {}


def test_subagent_state_persists_strict_json_and_rejects_legacy_nan(tmp_path):
    from nz_coder.runtime.agent import subagent

    state = subagent._new_subagent_state("parent-1", "explore", None)
    state["route_facts"] = {"score": float("nan")}
    subagent._save_subagent_state("parent-1", state, tmp_path)
    path = subagent._subagent_session_path(
        "parent-1",
        state["session_id"],
        tmp_path,
    )

    assert subagent._load_subagent_state(
        "parent-1",
        state["session_id"],
        tmp_path,
    )["route_facts"] == {"score": None}
    json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )

    path.write_text('{"session_id":"owned","score":NaN}', encoding="utf-8")
    assert subagent._load_subagent_state(
        "parent-1",
        state["session_id"],
        tmp_path,
    ) == {}


@pytest.mark.parametrize("mode", ["git", "copy", "direct", "unknown"])
def test_subagent_rejects_unowned_persisted_worktree(tmp_path, mode):
    from nz_coder.runtime.agent import subagent

    outside = tmp_path.parent / "unowned-subagent-worktree"
    outside.mkdir(exist_ok=True)
    state = subagent._new_subagent_state("parent-1", "general-purpose", None)
    state["worktree"] = {
        "id": state["session_id"],
        "path": str(outside),
        "mode": mode,
    }

    with pytest.raises(ValueError, match="worktree"):
        subagent._ensure_subagent_worktree(tmp_path, state)


def test_subagent_persists_usage_and_returns_only_invocation_cost_delta(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.tools import ToolOutput

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("first"), usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost": 0.25,
            }),
            FakeResponse(FakeMessage("second"), usage={
                "prompt_tokens": 40,
                "completion_tokens": 10,
                "total_tokens": 50,
                "cost": 0.10,
            }),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        first = subagent.run_subagent("inspect")
        child_session = _extract_subagent_session_id(first)
        second = subagent.run_subagent("continue", session_id=child_session)

        assert isinstance(first, ToolOutput)
        assert first.metadata["child_cost_delta"] == pytest.approx(0.25)
        assert first.metadata["child_total_cost"] == pytest.approx(0.25)
        assert second.metadata["child_cost_delta"] == pytest.approx(0.10)
        assert second.metadata["child_total_cost"] == pytest.approx(0.35)
        state_path = (
            tmp / ".nz-coder/sessions/_artifacts/main-session/subagents"
            / child_session / "state.json"
        )
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert "cost" not in persisted
        assert "cost_known" not in persisted
        assert "tokens" not in persisted
        assert "iterations" not in persisted
        assert persisted["child_result"]["cost"] == pytest.approx(0.35)
        assert persisted["child_result"]["usage"] == {
            "input": 140,
            "output": 30,
            "total": 170,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
        messages = _native_child_messages(persisted, "main-session", tmp)
        assistants = [
            message for message in messages
            if message.get("role") == "assistant"
        ]
        assert assistants[-1]["_nz_provider_id"]
        assert assistants[-1]["_nz_model_id"]
        assert assistants[-1]["_nz_parent_id"]
        assert assistants[-1]["_nz_usage"]["input"] == 40
        assert assistants[-1]["_nz_cost"] == pytest.approx(0.10)
        assert {part["type"] for part in assistants[-1]["_nz_parts"]} >= {
            "step-start",
            "step-finish",
            "text",
        }
        assert assistants[-1]["_nz_end_state"] == {"reason": "completed"}
        assert "_invocation_cost_before" not in persisted
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_provider_failure_persists_typed_assistant_error(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: FakeClient([]))

        def fail(*_args, **_kwargs):
            raise RuntimeError("invalid api key")

        monkeypatch.setattr(subagent, "_completion_with_timeout", fail)

        result = subagent.run_subagent("inspect")

        child_session = _extract_subagent_session_id(result)
        state = subagent._load_subagent_state("main-session", child_session, tmp)
        messages = _native_child_messages(state, "main-session", tmp)
        assistant = next(
            message for message in messages
            if message.get("role") == "assistant"
        )
        assert state["status"] == "error"
        assert assistant["_nz_assistant_error"]["name"] == "APIError"
        assert assistant["_nz_assistant_error"]["data"]["message"] == (
            "An internal error occurred."
        )
        assert "invalid api key" not in str(assistant)
        assert assistant["_nz_assistant_error"]["data"]["isRetryable"] is False
        assert assistant["_nz_finish"] == "error"
        assert assistant["_nz_end_state"] == {"reason": "errored"}
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_retries_transient_provider_error_on_same_assistant(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.verification.recovery import RecoveryState

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    attempts = []
    try:
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: FakeClient([]))
        monkeypatch.setattr(
            RecoveryState,
            "backoff_seconds",
            lambda _self, _error=None: 0.0,
        )

        def flaky(*_args, **_kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("temporarily unavailable")
            return FakeResponse(FakeMessage("recovered"))

        monkeypatch.setattr(subagent, "_completion_with_timeout", flaky)

        result = subagent.run_subagent("inspect")

        child_session = _extract_subagent_session_id(result)
        state = subagent._load_subagent_state("main-session", child_session, tmp)
        messages = _native_child_messages(state, "main-session", tmp)
        assistants = [
            message for message in messages
            if message.get("role") == "assistant"
        ]
        retries = [
            part for part in assistants[0]["_nz_parts"]
            if part.get("type") == "retry"
        ]
        assert len(attempts) == 3
        assert len(assistants) == 1
        assert [part["attempt"] for part in retries] == [1, 2]
        assert all(part["error"]["name"] == "APIError" for part in retries)
        assert assistants[0]["content"] == "recovered"
        assert assistants[0]["_nz_end_state"] == {"reason": "completed"}
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_fork_clones_referenced_child_identity_and_rewrites_task_part():
    from nz_coder.runtime.agent import subagent
    from nz_coder.protocol.message_schema import ensure_message_identities

    old, tmp = _tmp_workdir()
    try:
        source = subagent._new_subagent_state("parent-old", "explore", None)
        source["status"] = "completed"
        source["messages"] = [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": "done"},
        ]
        ensure_message_identities(source["messages"], source["session_id"])
        subagent._save_subagent_state("parent-old", source, tmp)
        parent_messages = [{
            "role": "assistant",
            "content": "",
            "_nz_parts": [{
                "id": "part-task",
                "message_id": "msg-parent",
                "type": "tool",
                "tool": "task",
                "call_id": "call-task",
                "state": {
                    "status": "completed",
                    "input": {"prompt": "inspect"},
                    "output": "done",
                    "title": "explore",
                    "metadata": {"child_session_id": source["session_id"]},
                    "time": {"start": 1, "end": 2},
                },
            }],
        }]

        mapping = subagent.clone_referenced_subagents(
            "parent-old",
            "parent-new",
            parent_messages,
            parent_agent_id="agent-parent-new",
            workspace_root=tmp,
        )

        target_id = mapping[source["session_id"]]
        metadata = parent_messages[0]["_nz_parts"][0]["state"]["metadata"]
        target = subagent._load_subagent_state("parent-new", target_id, tmp)
        assert metadata["child_session_id"] == target_id
        assert target["session_id"] != source["session_id"]
        assert target["agent_id"] != source["agent_id"]
        assert target["parent_session_id"] == "parent-new"
        assert target["parent_agent_id"] == "agent-parent-new"
        assert {message["_nz_session_id"] for message in target["messages"]} == {
            target_id
        }
        assert subagent._load_subagent_state(
            "parent-old", source["session_id"], tmp
        )["session_id"] == source["session_id"]
    finally:
        subagent.set_parent_session(None)
        _restore_workdir(old, tmp)


def test_fork_clones_write_child_changed_and_deleted_files():
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    try:
        (tmp / "app.py").write_text("parent\n", encoding="utf-8")
        (tmp / "deleted.py").write_text("parent\n", encoding="utf-8")
        source = subagent._new_subagent_state(
            "parent-old", "general-purpose", None
        )
        source["status"] = "completed"
        source["changed_files"] = ["app.py", "deleted.py"]
        old_worktree = tmp / ".nz-coder" / "worktrees" / source["session_id"]
        old_worktree.mkdir(parents=True)
        (old_worktree / "app.py").write_text("child\n", encoding="utf-8")
        source["worktree"] = {
            "id": source["session_id"],
            "path": str(old_worktree),
            "branch": "",
            "based_on": "HEAD",
            "head_commit": "",
            "mode": "copy",
        }
        subagent._save_subagent_state("parent-old", source, tmp)
        parent_messages = [{
            "_nz_parts": [{
                "type": "tool",
                "tool": "task",
                "state": {
                    "metadata": {"child_session_id": source["session_id"]}
                },
            }],
        }]

        mapping = subagent.clone_referenced_subagents(
            "parent-old", "parent-new", parent_messages, workspace_root=tmp
        )

        target = subagent._load_subagent_state(
            "parent-new", mapping[source["session_id"]], tmp
        )
        target_worktree = Path(target["worktree"]["path"])
        assert target["worktree"]["mode"] == "copy"
        assert target_worktree != old_worktree
        assert (target_worktree / "app.py").read_text(encoding="utf-8") == "child\n"
        assert not (target_worktree / "deleted.py").exists()
    finally:
        subagent.set_parent_session(None)
        _restore_workdir(old, tmp)


def test_fork_child_overlay_failure_preserves_source_and_removes_clone(monkeypatch):
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    try:
        (tmp / "app.py").write_text("parent\n", encoding="utf-8")
        source = subagent._new_subagent_state(
            "parent-old", "general-purpose", None
        )
        source["status"] = "completed"
        source["changed_files"] = ["app.py"]
        old_worktree = tmp / ".nz-coder" / "worktrees" / source["session_id"]
        old_worktree.mkdir(parents=True)
        old_file = old_worktree / "app.py"
        old_file.write_text("child\n", encoding="utf-8")
        source["worktree"] = {
            "id": source["session_id"],
            "path": str(old_worktree),
            "branch": "",
            "based_on": "HEAD",
            "head_commit": "",
            "mode": "copy",
        }
        subagent._save_subagent_state("parent-old", source, tmp)
        parent_messages = [{"_nz_parts": [{
            "type": "tool",
            "tool": "task",
            "state": {"metadata": {"child_session_id": source["session_id"]}},
        }]}]
        original_copy = subagent.shutil.copy2

        def fail_old_overlay(source_path, target_path, *args, **kwargs):
            if Path(source_path).resolve() == old_file.resolve():
                raise OSError("overlay failed")
            return original_copy(source_path, target_path, *args, **kwargs)

        monkeypatch.setattr(subagent.shutil, "copy2", fail_old_overlay)

        with pytest.raises(OSError, match="overlay failed"):
            subagent.clone_referenced_subagents(
                "parent-old", "parent-new", parent_messages, workspace_root=tmp
            )

        assert old_file.read_text(encoding="utf-8") == "child\n"
        assert parent_messages[0]["_nz_parts"][0]["state"]["metadata"][
            "child_session_id"
        ] == source["session_id"]
        worktrees = [
            path for path in (tmp / ".nz-coder" / "worktrees").iterdir()
            if path.is_dir()
        ]
        assert worktrees == [old_worktree]
    finally:
        subagent.set_parent_session(None)
        _restore_workdir(old, tmp)


def test_subagent_keeps_large_tool_output_when_request_capacity_can_hold_it(
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        output = "X" * 35000
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("read_file", {"path": "huge.txt"})])),
            FakeResponse(FakeMessage("done")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent, "_run_allowed_tool", lambda *args, **kwargs: output)

        result = subagent.run_subagent("read huge output")

        second_messages = fake.chat.completions.requests[1]["messages"]
        tool_msg = next(msg for msg in second_messages if msg.get("role") == "tool")
        assert tool_msg["content"] == output
        session_id = _extract_subagent_session_id(result)
        state = subagent._load_subagent_state("main-session", session_id, tmp)
        messages = _native_child_messages(state, "main-session", tmp)
        tool_assistant = next(
            message
            for message in messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        tool_part = next(
            part for part in tool_assistant["_nz_parts"] if part["type"] == "tool"
        )
        assert tool_part["tool"] == "read_file"
        assert tool_part["state"]["status"] == "completed"
        assert tool_part["state"]["output"] == output
        worktree = Path(state["worktree"]["path"])
        assert not (
            worktree / ".nz-coder" / "tool-results" / "subagent-call_1.txt"
        ).exists()
        assert "[Subagent status: completed]" in result
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_general_subagent_reports_verification_failure_and_rolls_back(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("write_file", {"path": "app.py", "content": "bad"})])),
            FakeResponse(FakeMessage("patched")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent, "_run_allowed_tool", lambda *args, **kwargs: "Created app.py")
        monkeypatch.setattr(subagent, "dispatch", lambda name, args: "FAIL: py_compile changed files\nSyntaxError")

        result = subagent.run_subagent("patch app.py", agent_type="general-purpose")

        assert "[Subagent status: verification_failed_rolled_back]" in result
        assert "SyntaxError" in result
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_can_request_parent_input_and_resume(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        first = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("message_parent", {"message": "Need exact file path", "reason": "ambiguous target"})
            ])),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: first)

        result = subagent.run_subagent("find the correct file")

        sub_session_id = _extract_subagent_session_id(result)
        assert "[Subagent status: needs_parent]" in result
        assert "Need exact file path" in result
        assert subagent._subagent_session_path("main-session", sub_session_id).exists()

        resumed = FakeClient([FakeResponse(FakeMessage("done after parent reply"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: resumed)

        result2 = subagent.run_subagent("Use src/app.py.", session_id=sub_session_id)

        user_messages = [
            msg["content"]
            for msg in resumed.chat.completions.requests[0]["messages"]
            if msg.get("role") == "user"
        ]
        assert user_messages[0] == "find the correct file"
        assert user_messages[-1] == "Use src/app.py."
        assert "[Subagent status: completed]" in result2
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_allowed_tools_can_be_restricted():
    from nz_coder.runtime.agent.subagent import _subagent_tools

    names = {spec["function"]["name"] for spec in _subagent_tools("general-purpose", allowed_tools=["read_file"])}

    assert names == {"read_file", "message_parent", "send_message"}


def test_subagent_drains_peer_mail_as_untrusted_synthetic_context(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        scoped_background_agent_manager,
    )

    manager = BackgroundAgentManager(tmp_path, "parent")
    sender = subagent._new_subagent_state("parent", "general-purpose", None)
    recipient = subagent._new_subagent_state("parent", "general-purpose", None)
    sender.update({"background": True, "status": "running"})
    recipient.update({"background": True, "status": "running"})
    manager._save(sender)
    manager._save(recipient)
    manager.send_message(
        sender=sender["session_id"],
        recipient=recipient["session_id"],
        content="Check api.py before editing.",
    )

    with scoped_background_agent_manager(manager):
        messages = subagent._drain_peer_messages(
            "parent",
            recipient["session_id"],
        )

    assert len(messages) == 1
    assert messages[0][SYNTHETIC_USER_KEY] is True
    assert messages[0]["_nz_peer_message"] is True
    assert "untrusted peer-provided" in messages[0]["content"]
    assert "Check api.py" in messages[0]["content"]


def test_subagent_drains_queued_worker_mail_as_coordinator_instruction(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        scoped_background_agent_manager,
    )

    manager = BackgroundAgentManager(tmp_path, "parent")
    child = subagent._new_subagent_state("parent", "general-purpose", None)
    child.update({"background": True, "status": "running", "isolation": "thread"})
    manager._save(child)
    manager.send_message(
        sender="worker",
        recipient=child["session_id"],
        content="Inspect widgets.py before changing the ordering algorithm.",
    )

    with scoped_background_agent_manager(manager):
        messages = subagent._drain_peer_messages(
            "parent",
            child["session_id"],
        )

    assert len(messages) == 1
    assert messages[0][SYNTHETIC_USER_KEY] is True
    assert messages[0]["_nz_coordinator_instruction"] is True
    assert "<coordinator-instruction" in messages[0]["content"]
    assert "Inspect widgets.py" in messages[0]["content"]


def test_completion_with_timeout_works_outside_main_thread():
    from nz_coder.runtime.agent.subagent import SubagentTimeout, _completion_with_timeout

    class SlowCompletions:
        def create(self, **kwargs):
            time.sleep(0.2)
            return object()

    class SlowClient:
        class Chat:
            completions = SlowCompletions()
        chat = Chat()

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_completion_with_timeout, SlowClient(), timeout_seconds=0.01)
        try:
            future.result(timeout=1)
        except SubagentTimeout:
            pass
        else:
            raise AssertionError("expected SubagentTimeout")


def test_general_subagent_tracks_scope_and_changed_files(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("write_file", {"path": "app.py", "content": "print('ok')\n"})])),
            FakeResponse(FakeMessage("patched")),
        ])
        original_dispatch = subagent.dispatch

        def fake_dispatch(name, args):
            if name == "verify_changed_files":
                return "OK: py_compile changed files"
            return original_dispatch(name, args)

        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent, "dispatch", fake_dispatch)

        result = subagent.run_subagent(
            "patch app.py",
            agent_type="general-purpose",
            target_paths=["app.py"],
        )

        sub_session_id = _extract_subagent_session_id(result)
        state = json.loads(
            subagent._subagent_session_path("main-session", sub_session_id, tmp).read_text(encoding="utf-8")
        )
        assert state["claimed_paths"] == ["app.py"]
        assert state["changed_files"] == ["app.py"]
        assert state["conflicts"] == []
        assert "[Subagent scope: app.py]" in result
        assert "[Subagent changed files: app.py]" in result
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_blocks_overlapping_active_write_scope(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        existing = subagent._new_subagent_state("main-session", "general-purpose", None)
        existing["status"] = "needs_parent"
        existing["claimed_paths"] = ["app.py"]
        subagent._save_subagent_state("main-session", existing, tmp)

        result = subagent.run_subagent(
            "edit app.py",
            agent_type="general-purpose",
            target_paths=["app.py"],
        )

        assert result.startswith("Subagent spawn blocked:")
        assert existing["session_id"] in result
        assert "Requested scope: app.py" in result
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_reports_completed_worktree_conflict(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.worktree import Worktree

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        subagent.set_parent_session("main-session")
        sibling_dir = tmp / ".nz-coder" / "worktrees" / "sibling"
        sibling_dir.mkdir(parents=True, exist_ok=True)
        sibling = subagent._new_subagent_state("main-session", "general-purpose", None)
        sibling["status"] = "completed"
        sibling["changed_files"] = ["app.py"]
        sibling["worktree"] = {
            "id": sibling["session_id"],
            "path": str(sibling_dir),
            "branch": f"subagent-{sibling['session_id']}",
            "based_on": "HEAD",
            "head_commit": "abc123",
            "mode": "git",
        }
        sibling["worktree_rel"] = str(sibling_dir.relative_to(tmp))
        subagent._save_subagent_state("main-session", sibling, tmp)

        child_dir = tmp / ".nz-coder" / "worktrees" / "child"
        child_dir.mkdir(parents=True, exist_ok=True)

        def fake_create(self, worktree_id: str, base_ref: str = "HEAD"):
            return Worktree(
                id=worktree_id,
                path=str(child_dir),
                branch=f"subagent-{worktree_id}",
                based_on=base_ref,
                head_commit="def456",
                mode="git",
            )

        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("write_file", {"path": "app.py", "content": "print('child')\n"})])),
            FakeResponse(FakeMessage("patched")),
        ])
        original_dispatch = subagent.dispatch

        def fake_dispatch(name, args):
            if name == "verify_changed_files":
                return "OK: py_compile changed files"
            return original_dispatch(name, args)

        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent.WorktreeManager, "create", fake_create)
        monkeypatch.setattr(subagent, "dispatch", fake_dispatch)

        result = subagent.run_subagent(
            "edit app.py",
            agent_type="general-purpose",
            target_paths=["app.py"],
        )

        sub_session_id = _extract_subagent_session_id(result)
        state = json.loads(
            subagent._subagent_session_path("main-session", sub_session_id, tmp).read_text(encoding="utf-8")
        )
        assert state["changed_files"] == ["app.py"]
        assert state["conflicts"]
        assert state["conflicts"][0]["session_id"] == sibling["session_id"]
        assert "[Subagent conflicts:" in result
        assert sibling["session_id"] in result
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_plan_and_legacy_aliases_are_read_only():
    from nz_coder.runtime.agent.subagent import _subagent_tools

    plan_names = {spec["function"]["name"] for spec in _subagent_tools("plan")}
    review_names = {spec["function"]["name"] for spec in _subagent_tools("review")}
    test_names = {spec["function"]["name"] for spec in _subagent_tools("test")}

    assert "bash" in plan_names
    assert "bash" in review_names
    assert "bash" in test_names
    assert "write_file" not in plan_names
    assert "edit_file" not in review_names
    assert "apply_patch" not in test_names


@pytest.mark.parametrize(
    ("state", "agent_type", "expected"),
    [
        ({}, "explore", "read_child"),
        ({}, "general-purpose", "write_child"),
        ({"background": True}, "explore", "background"),
        ({"background": True, "workflow_run_id": "wf-1"}, "general-purpose", "workflow"),
    ],
)
def test_child_runtime_profile_reflects_execution_surface(state, agent_type, expected):
    from nz_coder.runtime.agent.subagent import _child_runtime_profile

    assert _child_runtime_profile(state, agent_type) == expected


def test_subagent_persists_worktree_and_child_trace(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.worktree import Worktree

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    old_trace_enabled = config.TRACE_ENABLED
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    config.TRACE_ENABLED = True
    parent_tracer = FakeParentTracer()
    try:
        wt_dir = tmp / ".nz-coder" / "worktrees" / "subagent-test"
        wt_dir.mkdir(parents=True, exist_ok=True)

        def fake_create(self, worktree_id: str, base_ref: str = "HEAD"):
            return Worktree(
                id=worktree_id,
                path=str(wt_dir),
                branch=f"subagent-{worktree_id}",
                based_on=base_ref,
                head_commit="abc123",
                mode="git",
            )

        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent.WorktreeManager, "create", fake_create)
        subagent.bind_parent_context(
            session_id="main-session",
            tracer=parent_tracer,
            agent_id="parent-agent",
            trace_id="parent-trace",
            model_id="parent-model",
        )

        result = subagent.run_subagent("scan repo", agent_type="explore")

        sub_session_id = _extract_subagent_session_id(result)
        state = json.loads(
            subagent._subagent_session_path("main-session", sub_session_id, tmp).read_text(encoding="utf-8")
        )
        assert state["worktree"]["mode"] == "git"
        assert state["worktree_rel"].startswith(".nz-coder/worktrees/")
        assert Path(tmp / state["trace_rel"]).exists()
        trace_rows = [
            json.loads(line)
            for line in Path(tmp / state["trace_rel"]).read_text(encoding="utf-8").splitlines()
        ]
        assert any(row.get("event") == "agent_runner_enter" for row in trace_rows)
        assert any(event == "subagent_spawn" for event, _ in parent_tracer.events)
        assert any(event == "subagent_complete" and payload.get("status") == "completed" for event, payload in parent_tracer.events)
        assert "[Subagent worktree:" in result
        assert "[Subagent trace:" in result
    finally:
        subagent.bind_parent_context()
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        config.TRACE_ENABLED = old_trace_enabled
        _restore_workdir(old, tmp)

def test_run_subagent_async_completes(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        result = asyncio.run(subagent.run_subagent_async("scan repo", agent_type="explore"))

        assert "[Subagent status: completed]" in result
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_foreground_task_propagates_tool_cancel_into_child_tools(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.tools import current_tool_cancel_event, scoped_tool_cancellation

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    cancel_event = threading.Event()
    tool_started = threading.Event()
    observed = []
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("read_file", {"path": "README.md"}),
            ])),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        def slow_tool(*_args, **_kwargs):
            observed.append(current_tool_cancel_event())
            tool_started.set()
            cancel_event.wait(1)
            return "Error: Read cancelled"

        monkeypatch.setattr(subagent, "_run_allowed_tool", slow_tool)
        canceller = threading.Thread(
            target=lambda: (tool_started.wait(1), cancel_event.set()),
        )
        canceller.start()
        with scoped_tool_cancellation(cancel_event):
            result = subagent.run_subagent("inspect README", agent_type="explore")
        canceller.join(1)

        assert observed == [cancel_event]
        assert "[Subagent status: cancelled]" in result
        session_id = _extract_subagent_session_id(result)
        state = subagent._load_subagent_state("main-session", session_id, tmp)
        assert state["status"] == "cancelled"
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_provider_request_closes_on_parent_cancel():
    from nz_coder.runtime.agent.subagent import SubagentCancelled, _completion_with_timeout

    started = threading.Event()
    closed = threading.Event()
    cancel_event = threading.Event()

    class Completions:
        def create(self, **_kwargs):
            started.set()
            closed.wait(2)
            raise RuntimeError("connection closed")

    class Client:
        chat = type("Chat", (), {"completions": Completions()})()

        def close(self):
            closed.set()

    def cancel():
        assert started.wait(1)
        cancel_event.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    with pytest.raises(SubagentCancelled):
        _completion_with_timeout(
            Client(),
            timeout_seconds=30,
            cancel_event=cancel_event,
        )
    thread.join(1)

    assert closed.is_set()


def test_run_subagent_async_signals_and_settles_worker_on_cancel(monkeypatch):
    from nz_coder.runtime.agent import subagent

    started = threading.Event()
    settled = threading.Event()
    observed = []

    def worker(_prompt, **kwargs):
        cancel_event = kwargs["cancel_event"]
        observed.append(cancel_event)
        started.set()
        cancel_event.wait(2)
        settled.set()
        return "cancelled"

    monkeypatch.setattr(subagent, "run_subagent", worker)

    async def scenario():
        task = asyncio.create_task(subagent.run_subagent_async("work"))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert len(observed) == 1
    assert observed[0].is_set()
    assert settled.is_set()
