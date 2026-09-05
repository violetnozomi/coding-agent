"""Smoke test: verify imports and tool registration."""

import json
import os
import subprocess
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_imports():
    from nz_coder.foundation import config
    assert config.MODEL_ID
    assert config.WORKDIR


def test_agent_loop_import_registers_eval_tools():
    import nz_coder.loop  # noqa: F401
    from nz_coder.tools import get_specs

    names = {spec["function"]["name"] for spec in get_specs()}
    assert {"project_profile", "plan_verification", "analyze_impact", "analyze_project_requirements", "create_project_blueprint", "scaffold_project", "inspect_generated_project", "check_project_completeness", "plan_project_acceptance", "verify_project_build", "review_run_evidence"}.issubset(names)


def test_tool_registration():
    from nz_coder.tools import get_specs, TOOL_HANDLERS

    # Force tool imports
    import nz_coder.tools.bash       # noqa
    import nz_coder.tools.files      # noqa
    import nz_coder.tools.search     # noqa
    import nz_coder.tools.todo       # noqa
    import nz_coder.tools.question   # noqa
    import nz_coder.tools.repo_intel  # noqa
    import nz_coder.intelligence.project_profile   # noqa
    import nz_coder.intelligence.verification_planner  # noqa
    import nz_coder.intelligence.impact_analyzer   # noqa
    import nz_coder.intelligence.reviewer          # noqa
    import nz_coder.project_creation.requirement_analyzer  # noqa
    import nz_coder.project_creation.blueprint  # noqa
    import nz_coder.project_creation.templates  # noqa
    import nz_coder.project_creation.inspector  # noqa
    import nz_coder.project_creation.completeness  # noqa
    import nz_coder.project_creation.acceptance_planner  # noqa
    import nz_coder.project_creation.verifier  # noqa
    import nz_coder.runtime.agent.subagent          # noqa
    import nz_coder.state.memory            # noqa
    import nz_coder.state.skills            # noqa

    specs = get_specs()
    names = [s["function"]["name"] for s in specs]

    expected = ["bash", "read_file", "write_file", "write_files_batch", "edit_file",
                "apply_patch", "replace_lines", "list_directory", "grep_search", "glob_search",
                "todo", "question", "task", "save_memory", "list_memories",
                "delete_memory", "project_profile", "plan_verification",
                "analyze_impact", "analyze_project_requirements", "create_project_blueprint",
                "scaffold_project", "inspect_generated_project", "check_project_completeness",
                "plan_project_acceptance", "verify_project_build", "review_run_evidence",
                "load_optional_tools", "load_skill"]
    for e in expected:
        assert e in names, f"Missing tool: {e}"
        assert e in TOOL_HANDLERS, f"Missing handler: {e}"
    assert "smart_search" not in names

    print(f"OK: {len(specs)} tools registered: {names}")


def test_optional_tool_loader_registers_python_ast_pack():
    from nz_coder.tools import dispatch, get_specs

    code = (
        "import json\n"
        "import nz_coder.loop\n"
        "from nz_coder.tools import get_specs\n"
        "print(json.dumps(sorted(spec['function']['name'] for spec in get_specs())))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    names_before = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "load_optional_tools" in names_before
    assert "smart_search" not in names_before
    assert "python_symbol_check" not in names_before
    assert "python_structural_edit" not in names_before

    result = dispatch("load_optional_tools", {"packs": ["python_ast"]})
    names_after = [spec["function"]["name"] for spec in get_specs()]
    assert "python_ast" in result
    assert "python_symbol_check" in names_after
    assert "python_structural_edit" in names_after


def test_tool_dispatch():
    from nz_coder.tools import dispatch
    import nz_coder.tools.files  # noqa

    result = dispatch("list_directory", {"path": ".", "depth": 1})
    assert "Error" not in result or "not a directory" in result
    print(f"OK: list_directory returned {len(result)} chars")


def test_tool_dispatch_ignores_extra_arguments():
    """模型偶发多传说明字段时，不应让工具调用直接失败。"""
    from nz_coder.tools import dispatch, register

    def echo(value: str) -> str:
        return value

    register(
        name="_test_echo",
        description="test helper",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=echo,
    )

    assert dispatch("_test_echo", {"value": "ok", "description": "ignored"}) == "ok"
    assert "missing" in dispatch("_test_echo", {"description": "ignored"})


def test_todo():
    from nz_coder.tools.todo import todo_update, has_open_items

    result = todo_update([
        {"content": "Step 1", "status": "completed"},
        {"content": "Step 2", "status": "in_progress"},
        {"content": "Step 3", "status": "pending"},
    ])
    assert "[x]" in result
    assert "[>]" in result
    assert "[ ]" in result
    assert has_open_items()
    print("OK: todo system works")


def test_permissions():
    from nz_coder.permissions import PermissionManager

    pm = PermissionManager("auto")
    assert pm.check("read_file", {"path": "test.py"})["behavior"] == "allow"
    assert pm.check("bash", {"command": "ls"})["behavior"] == "allow"
    assert pm.check("bash", {"command": "sudo rm -rf /"})["behavior"] == "deny"

    pm_plan = PermissionManager("plan")
    assert pm_plan.check("write_file", {"path": "x", "content": "y"})["behavior"] == "deny"
    assert pm_plan.check("replace_lines", {"path": "x", "start_line": 1, "end_line": 1, "new_text": "y"})["behavior"] == "deny"
    assert pm_plan.check("python_structural_edit", {"path": "x.py"})["behavior"] == "deny"
    assert pm_plan.check("read_file", {"path": "x"})["behavior"] == "allow"
    assert pm_plan.check("bash", {"command": "ls"})["behavior"] == "allow"
    assert pm_plan.check("bash", {"command": "echo hi > x.txt"})["behavior"] == "deny"
    print("OK: permission system works")


def test_prompt_builder():
    from nz_coder.runtime.conversation.prompt import build

    prompt = build(memory_block="## Test memory", skill_descriptions="- test-skill: desc")
    assert "NZ-Coder" in prompt
    assert "Test memory" in prompt
    assert "test-skill" in prompt
    assert "analyze_project_requirements" in prompt
    assert "load_optional_tools" in prompt
    assert "do not start with grep_search unless you are intentionally reusing local code" in prompt.lower()
    assert "same-basename file in a different directory" in prompt
    assert "Missing requested tests means the task is not complete." in prompt
    assert "Before finalizing a code-changing task, call review_run_evidence" in prompt
    print("OK: prompt builder works")


def test_transaction_commit():
    import tempfile
    from nz_coder.state.transaction import TransactionManager
    from nz_coder.foundation import config

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        txn = TransactionManager()
        # Write an original file
        test_file = tmpdir / "txn_test.txt"
        test_file.write_text("original")

        txn.begin()
        txn.track("txn_test.txt")
        from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
        WorkspaceFileAccess(tmpdir).write_text("txn_test.txt", "modified", transaction=txn)
        txn.commit()

        # After commit, the modified content should remain
        assert test_file.read_text() == "modified"
        print("OK: transaction commit works")
    finally:
        config.WORKDIR = old_workdir
        import shutil
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_transaction_rollback():
    import tempfile
    from nz_coder.state.transaction import TransactionManager
    from nz_coder.foundation import config

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        txn = TransactionManager()
        test_file = tmpdir / "txn_test.txt"
        test_file.write_text("original")

        txn.begin()
        txn.track("txn_test.txt")
        from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
        WorkspaceFileAccess(tmpdir).write_text("txn_test.txt", "bad change", transaction=txn)
        report = txn.rollback()

        # After rollback, original content should be restored
        assert test_file.read_text() == "original"
        assert "Restored" in report
        print("OK: transaction rollback works")
    finally:
        config.WORKDIR = old_workdir
        import shutil
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_transaction_rollback_new_file():
    import tempfile
    from nz_coder.state.transaction import TransactionManager
    from nz_coder.foundation import config

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        txn = TransactionManager()

        txn.begin()
        txn.track("new_file.txt")
        from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
        WorkspaceFileAccess(tmpdir).write_text("new_file.txt", "should be deleted", transaction=txn)
        report = txn.rollback()

        # New file should be deleted on rollback
        assert not (tmpdir / "new_file.txt").exists()
        assert "Deleted" in report
        print("OK: transaction rollback deletes new files")
    finally:
        config.WORKDIR = old_workdir
        import shutil
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_apply_patch_tool():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.tools.files import apply_patch, replace_lines

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        fp = tmpdir / "sample.py"
        fp.write_text("a = 1\nb = 2\n", encoding="utf-8")
        result = apply_patch([{
            "path": "sample.py",
            "old_text": "b = 2",
            "new_text": "b = 3",
        }])
        assert "Applied patch" in result
        assert "-b = 2" in result
        assert "+b = 3" in result
        assert fp.read_text(encoding="utf-8") == "a = 1\nb = 3\n"
        preview = apply_patch([{
            "op": "create",
            "path": "created.txt",
            "content": "new",
        }], dry_run=True)
        assert "Patch preview" in preview
        assert not (tmpdir / "created.txt").exists()
        created = apply_patch([{
            "op": "create",
            "path": "created.txt",
            "content": "new",
        }])
        assert "Applied patch" in created
        assert (tmpdir / "created.txt").read_text(encoding="utf-8") == "new"
        deleted = apply_patch([{
            "op": "delete",
            "path": "created.txt",
            "old_text": "new",
        }])
        assert "Applied patch" in deleted
        assert not (tmpdir / "created.txt").exists()
        failed = apply_patch([{
            "path": "sample.py",
            "old_text": "missing b = 3",
            "new_text": "x",
        }])
        assert "Nearby context" in failed
        line_edit = replace_lines("sample.py", 2, 2, "b = 4")
        assert "Replaced lines 2-2" in line_edit
        assert fp.read_text(encoding="utf-8") == "a = 1\nb = 4\n"
        print("OK: apply_patch tool works")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_memory_manager_crud():
    import tempfile
    import shutil
    from nz_coder.state.memory import MemoryManager

    tmpdir = Path(tempfile.mkdtemp())
    try:
        mgr = MemoryManager(tmpdir)
        assert "Saved memory" in mgr.save("Project Fact", "A useful fact", "project", "Details")
        assert "Project Fact" in mgr.list_memories()
        block = mgr.build_prompt_block()
        assert "background notes" in block  # CHANGED: 与新版 build_prompt_block 文案一致
        assert "Deleted memory" in mgr.delete("Project Fact")
        assert "No memories saved" in mgr.list_memories()
        print("OK: memory CRUD works")
    finally:
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_command_policy():
    from nz_coder.tool_platform.command_policy import classify_bash, is_known_read_only_command

    assert is_known_read_only_command("git status")
    assert not classify_bash("rg copy")["mutating"]
    assert classify_bash("echo hi > x.txt")["mutating"]
    assert not classify_bash("python -m pytest >/dev/null")["dangerous"]
    assert classify_bash("python3 -m pip install legacy-cgi")["reason"] == "package install"
    assert classify_bash("sudo rm -rf /")["dangerous"]
    print("OK: command policy works")


def test_bash_blocks_package_install_by_default(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools.bash import run_bash

    old_allow = config.ALLOW_BASH_PACKAGE_INSTALLS
    config.ALLOW_BASH_PACKAGE_INSTALLS = False
    monkeypatch.setattr(
        "nz_coder.tools.bash.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    try:
        result = run_bash("python3 -m pip install legacy-cgi")
    finally:
        config.ALLOW_BASH_PACKAGE_INSTALLS = old_allow

    assert result.startswith("Error: Package install blocked")


def test_subagent_stops_after_configured_turn_budget(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    class FakeFunction:
        name = "grep_search"
        arguments = '{"pattern": "__unlikely_subagent_test_pattern__", "path": "."}'

    class FakeToolCall:
        id = "call_1"
        function = FakeFunction()

    class FakeMessage:
        content = ""
        tool_calls = [FakeToolCall()]

        def model_dump(self):
            return {
                "role": "assistant",
                "content": self.content,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "grep_search",
                        "arguments": FakeFunction.arguments,
                    },
                }],
            }

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: FakeClient())
    try:
        result = subagent.run_subagent("find a symbol")
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout

    assert result.startswith("Subagent stopped: max turns reached")


def test_change_tracker_diff_and_revert():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.changes import ChangeTracker

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        fp = tmpdir / "tracked.txt"
        fp.write_text("before\n", encoding="utf-8")
        tracker = ChangeTracker(change_dir=tmpdir / ".changes")
        tracker.record_before("tracked.txt", True, "before\n")
        fp.write_text("after\n", encoding="utf-8")
        tracker.record_after("tracked.txt", True, "after\n")

        diff = tracker.render_diff()
        assert "-before" in diff
        assert "+after" in diff
        result = tracker.revert()
        assert "Reverted agent changes" in result
        assert fp.read_text(encoding="utf-8") == "before\n"
        print("OK: change tracker diff/revert works")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_session_save_load():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.sessions import load_session, save_session

    old_session_dir = config.SESSION_DIR
    tmpdir = Path(tempfile.mkdtemp())
    config.SESSION_DIR = tmpdir
    try:
        path = save_session([{"role": "user", "content": "hello"}], mode="plan", session_id="test_session")
        assert path.exists()
        payload = load_session("test_session")
        assert payload["mode"] == "plan"
        assert payload["messages"][0]["content"] == "hello"
        latest = load_session("latest")
        assert latest["session_id"] == "test_session"
        print("OK: session save/load works")
    finally:
        config.SESSION_DIR = old_session_dir
        shutil.rmtree(str(tmpdir), ignore_errors=True)



def test_session_artifact_paths_are_isolated_by_session_id():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.sessions import activate_session, active_session_id, session_runtime_state_path, session_subagent_dir

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        activate_session("session-one")
        activate_session("session-two")
        assert active_session_id() == "session-two"
        assert session_runtime_state_path("session-one") != session_runtime_state_path("session-two")
        assert session_subagent_dir("session-one") != session_subagent_dir("session-two")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_workspace_status_report():
    from nz_coder.state.workspace import status_report

    report = status_report(history=[{"role": "user", "content": "hi"}])
    assert "NZ-Coder Status" in report
    assert "Conversation messages: 1" in report
    print("OK: workspace status report works")


def test_persist_large_output_uses_current_workdir():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.context import persist_large_output

    old_workdir = config.WORKDIR
    old_trigger = config.PERSIST_OUTPUT_TRIGGER
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        result = persist_large_output("call_test", "x" * (old_trigger + 1))
        assert "Full output artifact: artifact_" in result
        assert str(tmpdir) not in result
        print("OK: large outputs use current WORKDIR")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_python_symbol_check_tool():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.tools.python_ast import python_symbol_check

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        (tmpdir / "user_manager.py").write_text('''def validate_email(email):
    return bool(email)

class UserManager:
    def create_user(self, email):
        if not validate_email(email):
            raise ValueError("bad")
        return email
''', encoding="utf-8")
        result = python_symbol_check(
            "user_manager.py",
            symbols=["validate_email", "UserManager", "UserManager.create_user"],
            calls=[{"caller": "UserManager.create_user", "callee": "validate_email"}],
        )
        assert result.startswith("OK:")
        assert "module-level function validate_email found" in result
        assert "UserManager.create_user calls validate_email" in result
        missing = python_symbol_check("user_manager.py", symbols=["missing"])
        assert missing.startswith("FAIL:")
        print("OK: python_symbol_check tool works")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_python_structural_edit_tool():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.tools.python_ast import python_structural_edit, python_symbol_check

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        fp = tmpdir / "user_manager.py"
        fp.write_text('''class UserManager:
    def __init__(self):
        self.users = {}

    def create_user(self, name, email):
        if not email:
            raise ValueError("Invalid email")
        self.users[email] = {"name": name, "email": email}
        return self.users[email]
''', encoding="utf-8")
        result = python_structural_edit(
            "user_manager.py",
            insertions=[{
                "before_symbol": "UserManager",
                "code": '''def validate_email(email):
    return bool(email and "@" in email and "." in email.split("@")[-1])
''',
            }],
            replacements=[{
                "target": "UserManager.create_user",
                "code": '''def create_user(self, name, email):
    if not validate_email(email):
        raise ValueError("Invalid email")
    self.users[email] = {"name": name, "email": email}
    return self.users[email]
''',
            }],
        )
        assert "Applied Python structural edit" in result
        content = fp.read_text(encoding="utf-8")
        assert content.startswith("def validate_email")
        assert "def create_user(self, name, email):" in content
        check = python_symbol_check(
            "user_manager.py",
            symbols=["validate_email", "UserManager.create_user"],
            calls=[{"caller": "UserManager.create_user", "callee": "validate_email"}],
        )
        assert check.startswith("OK:")
        print("OK: python_structural_edit tool works")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_benchmark_tasks_defined():
    from nz_coder.benchmark import ALL_TASKS, TASK_MAP, render_markdown_report, summarize_results

    assert len(ALL_TASKS) >= 13
    assert "fizzbuzz" in TASK_MAP
    assert "bugfix_sum" in TASK_MAP
    assert "refactor_class" in TASK_MAP
    fake_results = [
        {"task_id": "a", "difficulty": "easy", "task_type": "bugfix", "passed": True, "turns": 1, "tool_calls": 2, "duration": 0.1, "reason": "ok"},
        {"task_id": "b", "difficulty": "hard", "task_type": "refactor", "passed": False, "turns": 2, "tool_calls": 3, "duration": 0.2, "reason": "file missing", "failure_category": "missing_artifact"},
    ]
    summary = summarize_results(fake_results)
    assert summary["by_difficulty"]["easy"]["passed"] == 1
    report_md = render_markdown_report({
        "timestamp": "now",
        "model": "fake",
        "total": 2,
        "passed": 1,
        "pass_rate": "50%",
        "summary": summary,
        "results": fake_results,
    })
    assert "Benchmark Report" in report_md
    print(f"OK: {len(ALL_TASKS)} benchmark tasks defined")


def test_external_benchmark_helpers_parse_args():
    from nz_coder.aider_benchmark import build_parser as build_aider_parser
    from nz_coder.swebench_lite import build_parser as build_swe_parser

    aider_args = build_aider_parser().parse_args(["official-command", "--num-tests", "1"])
    swe_args = build_swe_parser().parse_args(["check"])
    assert aider_args.command == "official-command"
    assert aider_args.num_tests == 1
    assert swe_args.command == "check"
    print("OK: external benchmark helpers parse args")



class _FakeLive:
    def __init__(
        self,
        renderable,
        console=None,
        auto_refresh=False,
        screen=False,
        transient=False,
        vertical_overflow=None,
    ):
        self.renderable = renderable
        self.console = console
        self.auto_refresh = auto_refresh
        self.screen = screen
        self.transient = transient
        self.vertical_overflow = vertical_overflow
        self.is_started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.update_calls = []

    def start(self):
        self.is_started = True
        self.start_calls += 1

    def stop(self):
        self.is_started = False
        self.stop_calls += 1

    def update(self, renderable, refresh=False):
        self.renderable = renderable
        plain = getattr(renderable, "plain", str(renderable))
        self.update_calls.append((plain, refresh))


class _FakeConsole:
    def __init__(self, printer=None):
        self.is_terminal = True
        self.size = type("_Size", (), {"height": 24})()
        self._printer = printer or (lambda *args, **kwargs: None)
        self.width = 100

    def print(self, *args, **kwargs):
        self._printer(*args, **kwargs)


def test_surface_console_falls_back_to_rich_after_terminal_boundary_failure():
    from nz_coder.interface.cli import _SurfaceConsole

    projected = []
    terminal = []
    printed = []

    class _Surface:
        def append_output(self, value):
            projected.append(value)

        def append_notice(self, value):
            terminal.append(value)

    surface = _Surface()
    output = _SurfaceConsole(
        _FakeConsole(lambda *args, **kwargs: printed.append((args, kwargs))),
        surface,
    )

    output.print("inside")
    output.print_terminal("terminal")
    output.disable_surface()
    output.print("fallback", markup=False)

    assert projected and "inside" in projected[0]
    assert terminal and "terminal" in terminal[0]
    assert printed == [(('fallback',), {'markup': False})]


class _FakeRenderer:
    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")


class _StubClient:
    class _Chat:
        class _Completions:
            def create(self, **kwargs):
                raise AssertionError("LLM should not be called in this test")

        def __init__(self):
            self.completions = self._Completions()

    def __init__(self):
        self.chat = self._Chat()


def test_streaming_renderer_updates_live_in_place(monkeypatch):
    from nz_coder.interface import cli as cli_mod

    printed = []
    monkeypatch.setattr(cli_mod, "Live", _FakeLive)
    ticks = iter([0.1, 0.1, 0.2, 0.2])
    monkeypatch.setattr(cli_mod.time, "monotonic", lambda: next(ticks))
    renderer = cli_mod.StreamingRenderer(
        live_console=_FakeConsole(lambda *args, **kwargs: printed.append((args, kwargs)))
    )
    renderer.start()
    live = renderer._live

    assert live is not None
    renderer.on_token("hello")
    renderer.on_token(" world")
    renderer.on_token(None)

    assert live.update_calls[0] == ("hello", True)
    assert live.update_calls[-1] == ("hello world", True)
    assert live.stop_calls == 1
    assert len(printed) == 1


def test_streaming_renderer_resume_restores_buffer(monkeypatch):
    from nz_coder.interface import cli as cli_mod

    monkeypatch.setattr(cli_mod, "Live", _FakeLive)
    renderer = cli_mod.StreamingRenderer(live_console=_FakeConsole())
    renderer.start()
    first_live = renderer._live

    assert first_live is not None
    renderer.on_token("hello")

    renderer.pause()
    renderer.pause()
    assert first_live.stop_calls == 1
    assert renderer._live is None

    renderer.resume()
    assert renderer._live is None

    renderer.resume()
    second_live = renderer._live

    assert second_live is not None
    assert second_live is not first_live
    assert second_live.start_calls == 1
    assert second_live.update_calls[-1] == ("hello", True)
    renderer.finish()
    assert second_live.stop_calls == 1


def test_streaming_renderer_status_is_transient(monkeypatch):
    from nz_coder.interface import cli as cli_mod

    printed = []
    monkeypatch.setattr(cli_mod, "Live", _FakeLive)
    renderer = cli_mod.StreamingRenderer(
        live_console=_FakeConsole(lambda *args, **kwargs: printed.append((args, kwargs)))
    )
    renderer.start()
    renderer.set_status(("⠋ bash · pytest -q", "  12 passed"))
    renderer.on_token("final answer")
    renderer._refresh()
    live = renderer._live
    assert live is not None
    assert "bash · pytest -q" in live.update_calls[-1][0]
    assert "final answer" in live.update_calls[-1][0]

    renderer.finish()

    assert len(printed) == 1
    assert "bash · pytest -q" not in str(printed[0])


def test_cli_drains_multiline_paste():
    from io import StringIO
    from nz_coder.interface.cli import _drain_pasted_lines

    stream = StringIO("1. add validation\n2. add tests\n")
    assert _drain_pasted_lines(stdin=stream, is_ready=lambda timeout: True) == [
        "1. add validation",
        "2. add tests",
    ]

def test_permission_manager_pauses_renderer_around_input(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    renderer = _FakeRenderer()
    monkeypatch.setattr(builtins, "input", lambda prompt: "y")

    pm = PermissionManager("default", renderer=renderer)
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is True
    assert renderer.calls == ["pause", "resume"]


def test_permission_manager_uses_renderer_console_input(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    class _RendererWithConsole:
        def __init__(self):
            self.console = _FakeConsole()
            self.calls = []

        def pause(self):
            self.calls.append("pause")

        def resume(self):
            self.calls.append("resume")

    renderer = _RendererWithConsole()
    console_calls = []
    renderer.console.input = lambda prompt, markup=False: console_calls.append((prompt, markup)) or "y"

    def _unexpected_input(prompt):
        raise AssertionError("builtins.input should not be used when renderer.console.input is available")

    monkeypatch.setattr(builtins, "input", _unexpected_input)

    pm = PermissionManager("default", renderer=renderer)
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is True
    assert renderer.calls == ["pause", "resume"]
    assert console_calls == [("  Allow? (y/n/a=always/p=always-prefix): ", False)]


def test_permission_manager_retries_blank_input(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    answers = iter(["", "y"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))
    monkeypatch.setattr(PermissionManager, "_tty_input", lambda self, prompt: (_ for _ in ()).throw(AssertionError("tty fallback should not run for blank input retry")))

    pm = PermissionManager("default")
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is True



def test_permission_manager_retries_invalid_input(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    answers = iter(["1. leftover pasted requirement", "y"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))
    monkeypatch.setattr(PermissionManager, "_tty_input", lambda self, prompt: (_ for _ in ()).throw(EOFError()))

    pm = PermissionManager("default")
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is True

def test_permission_manager_falls_back_after_console_eof(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    class _RendererWithConsole:
        def __init__(self):
            self.console = _FakeConsole()
            self.calls = []

        def pause(self):
            self.calls.append("pause")

        def resume(self):
            self.calls.append("resume")

    renderer = _RendererWithConsole()
    renderer.console.input = lambda prompt, markup=False: (_ for _ in ()).throw(EOFError())
    monkeypatch.setattr(builtins, "input", lambda prompt: "y")

    pm = PermissionManager("default", renderer=renderer)
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is True
    assert renderer.calls == ["pause", "resume"]


def test_permission_manager_resumes_renderer_after_eof(monkeypatch):
    import builtins
    from nz_coder.permissions import PermissionManager

    renderer = _FakeRenderer()

    def _raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _raise_eof)
    monkeypatch.setattr(PermissionManager, "_tty_input", lambda self, prompt: (_ for _ in ()).throw(EOFError()))

    pm = PermissionManager("default", renderer=renderer)
    assert pm.ask_user("write_file", {"path": "app.py", "content": "print(\"ok\")\n"}) is False
    assert renderer.calls == ["pause", "resume"]



def test_agent_loop_keeps_renderer_reference():
    from nz_coder.loop import AgentLoop

    renderer = _FakeRenderer()
    agent = AgentLoop("test", permission_mode="auto", client=_StubClient(), trace_enabled=False, renderer=renderer)

    assert agent.renderer is renderer
    assert agent.permissions._renderer is renderer


def test_glob_search_recurses_into_subdirectories(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.search import glob_search

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        nested = tmp_path / "test"
        nested.mkdir()
        (nested / "demo.py").write_text("print(1)\n", encoding="utf-8")

        result = glob_search("**/*.py")

        assert "test/demo.py" in result
    finally:
        config.WORKDIR = old


def test_list_directory_includes_subdirectories(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.files import list_directory

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "test").mkdir()
        result = list_directory(".", depth=1)
        assert "test/" in result
        assert "(empty)" not in result
    finally:
        config.WORKDIR = old


def test_prompt_forbids_bash_redirection_for_file_writes():
    from nz_coder.runtime.conversation.prompt import build

    prompt = build()

    assert "always use write_file or write_files_batch" in prompt
    assert "cat > file" in prompt
    assert "echo ... > file" in prompt

if __name__ == "__main__":
    test_imports()
    test_tool_registration()
    test_tool_dispatch()
    test_todo()
    test_permissions()
    test_prompt_builder()
    test_transaction_commit()
    test_transaction_rollback()
    test_transaction_rollback_new_file()
    test_apply_patch_tool()
    test_memory_manager_crud()
    test_command_policy()
    test_change_tracker_diff_and_revert()
    test_session_save_load()
    test_workspace_status_report()
    test_persist_large_output_uses_current_workdir()
    test_python_symbol_check_tool()
    test_python_structural_edit_tool()
    test_benchmark_tasks_defined()
    test_external_benchmark_helpers_parse_args()
    print("\nAll smoke tests passed!")


def test_session_runtime_dirs_are_isolated():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.sessions import (
        activate_session,
        session_change_dir,
        session_tool_results_dir,
        session_trace_dir,
        session_transcript_dir,
    )

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        activate_session("session-one")
        one_trace = session_trace_dir()
        one_change = session_change_dir()
        one_tools = session_tool_results_dir()
        one_transcripts = session_transcript_dir()

        activate_session("session-two")
        assert session_trace_dir() != one_trace
        assert session_change_dir() != one_change
        assert session_tool_results_dir() != one_tools
        assert session_transcript_dir() != one_transcripts
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_scratchpad_isolated_by_session_id():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.sessions import activate_session
    from nz_coder.tools.scratchpad import scratchpad

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        activate_session("session-one")
        scratchpad.clear()
        scratchpad.update("finding", "one")

        activate_session("session-two")
        scratchpad.clear()
        assert scratchpad.read() == "Scratchpad is empty."
        scratchpad.update("finding", "two")

        activate_session("session-one")
        assert "one" in scratchpad.read()
        assert "two" not in scratchpad.read()
    finally:
        activate_session("session-one")
        scratchpad.clear()
        activate_session("session-two")
        scratchpad.clear()
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_todo_isolated_by_session_id():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.sessions import activate_session
    from nz_coder.tools.todo import render, todo_update

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        activate_session("session-one")
        todo_update([{"content": "first", "status": "in_progress"}])

        activate_session("session-two")
        assert render() == "No todos."
        todo_update([{"content": "second", "status": "pending"}])

        activate_session("session-one")
        assert "first" in render()
        assert "second" not in render()
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_persist_large_output_uses_active_session_runtime_dir():
    import tempfile
    import shutil
    from nz_coder.foundation import config
    from nz_coder.state.context import persist_large_output
    from nz_coder.state.sessions import activate_session, session_tool_results_dir

    old_workdir = config.WORKDIR
    old_trigger = config.PERSIST_OUTPUT_TRIGGER
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        activate_session("session-output")
        result = persist_large_output("call_test", "x" * (old_trigger + 1))
        persisted = list(session_tool_results_dir("session-output").glob("artifact_*.txt"))
        assert len(persisted) == 1
        assert "Full output artifact: artifact_" in result
        assert str(persisted[0]) not in result
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)
