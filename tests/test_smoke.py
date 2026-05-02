"""Smoke test: verify imports and tool registration."""

import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_imports():
    from nz_coder import config
    assert config.MODEL_ID
    assert config.WORKDIR


def test_tool_registration():
    from nz_coder.tools import get_specs, TOOL_HANDLERS

    # Force tool imports
    import nz_coder.tools.bash       # noqa
    import nz_coder.tools.files      # noqa
    import nz_coder.tools.python_ast  # noqa
    import nz_coder.tools.search     # noqa
    import nz_coder.tools.todo       # noqa
    import nz_coder.subagent          # noqa
    import nz_coder.memory            # noqa
    import nz_coder.skills            # noqa

    specs = get_specs()
    names = [s["function"]["name"] for s in specs]

    expected = ["bash", "read_file", "write_file", "edit_file",
                "apply_patch", "python_symbol_check", "python_structural_edit",
                "list_directory", "grep_search", "glob_search",
                "todo", "task", "save_memory", "list_memories",
                "delete_memory", "load_skill"]
    for e in expected:
        assert e in names, f"Missing tool: {e}"
        assert e in TOOL_HANDLERS, f"Missing handler: {e}"

    print(f"OK: {len(specs)} tools registered: {names}")


def test_tool_dispatch():
    from nz_coder.tools import dispatch
    import nz_coder.tools.files  # noqa

    result = dispatch("list_directory", {"path": ".", "depth": 1})
    assert "Error" not in result or "not a directory" in result
    print(f"OK: list_directory returned {len(result)} chars")


def test_todo():
    from nz_coder.tools.todo import todo_update, render, has_open_items

    result = todo_update([
        {"content": "Step 1", "status": "completed"},
        {"content": "Step 2", "status": "in_progress"},
        {"content": "Step 3", "status": "pending"},
    ])
    assert "[x]" in result
    assert "[>]" in result
    assert "[ ]" in result
    assert has_open_items()
    print(f"OK: todo system works")


def test_permissions():
    from nz_coder.permissions import PermissionManager

    pm = PermissionManager("auto")
    assert pm.check("read_file", {"path": "test.py"})["behavior"] == "allow"
    assert pm.check("bash", {"command": "ls"})["behavior"] == "allow"
    assert pm.check("bash", {"command": "sudo rm -rf /"})["behavior"] == "deny"

    pm_plan = PermissionManager("plan")
    assert pm_plan.check("write_file", {"path": "x", "content": "y"})["behavior"] == "deny"
    assert pm_plan.check("python_structural_edit", {"path": "x.py"})["behavior"] == "deny"
    assert pm_plan.check("read_file", {"path": "x"})["behavior"] == "allow"
    assert pm_plan.check("bash", {"command": "ls"})["behavior"] == "allow"
    assert pm_plan.check("bash", {"command": "echo hi > x.txt"})["behavior"] == "deny"
    print("OK: permission system works")


def test_prompt_builder():
    from nz_coder.prompt import build

    prompt = build(memory_block="## Test memory", skill_descriptions="- test-skill: desc")
    assert "NZ-Coder" in prompt
    assert "Test memory" in prompt
    assert "test-skill" in prompt
    print("OK: prompt builder works")


def test_transaction_commit():
    import tempfile, os
    from nz_coder.transaction import TransactionManager
    from nz_coder import config

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
        test_file.write_text("modified")
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
    from nz_coder.transaction import TransactionManager
    from nz_coder import config

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        txn = TransactionManager()
        test_file = tmpdir / "txn_test.txt"
        test_file.write_text("original")

        txn.begin()
        txn.track("txn_test.txt")
        test_file.write_text("bad change")
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
    from nz_coder.transaction import TransactionManager
    from nz_coder import config

    old_workdir = config.WORKDIR
    tmpdir = Path(tempfile.mkdtemp())
    config.WORKDIR = tmpdir
    try:
        txn = TransactionManager()

        txn.begin()
        txn.track("new_file.txt")
        (tmpdir / "new_file.txt").write_text("should be deleted")
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
    from nz_coder import config
    from nz_coder.tools.files import apply_patch

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
        print("OK: apply_patch tool works")
    finally:
        config.WORKDIR = old_workdir
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_memory_manager_crud():
    import tempfile
    import shutil
    from nz_coder.memory import MemoryManager

    tmpdir = Path(tempfile.mkdtemp())
    try:
        mgr = MemoryManager(tmpdir)
        assert "Saved memory" in mgr.save("Project Fact", "A useful fact", "project", "Details")
        assert "Project Fact" in mgr.list_memories()
        block = mgr.build_prompt_block()
        assert "untrusted" in block
        assert "Deleted memory" in mgr.delete("Project Fact")
        assert "No memories saved" in mgr.list_memories()
        print("OK: memory CRUD works")
    finally:
        shutil.rmtree(str(tmpdir), ignore_errors=True)


def test_command_policy():
    from nz_coder.command_policy import classify_bash, is_known_read_only_command

    assert is_known_read_only_command("git status")
    assert not classify_bash("rg copy")["mutating"]
    assert classify_bash("echo hi > x.txt")["mutating"]
    assert classify_bash("sudo rm -rf /")["dangerous"]
    print("OK: command policy works")


def test_change_tracker_diff_and_revert():
    import tempfile
    import shutil
    from nz_coder import config
    from nz_coder.changes import ChangeTracker

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
    from nz_coder import config
    from nz_coder.sessions import load_session, save_session

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


def test_workspace_status_report():
    from nz_coder.workspace import status_report

    report = status_report(history=[{"role": "user", "content": "hi"}])
    assert "NZ-Coder Status" in report
    assert "Conversation messages: 1" in report
    print("OK: workspace status report works")


def test_python_symbol_check_tool():
    import tempfile
    import shutil
    from nz_coder import config
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
    from nz_coder import config
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
    test_python_symbol_check_tool()
    test_python_structural_edit_tool()
    test_benchmark_tasks_defined()
    print("\nAll smoke tests passed!")
