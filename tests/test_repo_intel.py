"""Tests for repository intelligence tools."""
from __future__ import annotations

import subprocess

import pytest


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_smart_search_uses_valid_git_grep_pathspec(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools import repo_intel

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "pkg" / "module.py"
        target.parent.mkdir()
        target.write_text("class AgentLoop:\n    pass\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "grep", "-l"]:
                calls.append(cmd)
                assert "--include" not in cmd
                assert "--" in cmd
                assert any(arg.endswith("*.py") for arg in cmd)
                return subprocess.CompletedProcess(cmd, 0, "pkg/module.py\n", "")
            raise AssertionError(f"unexpected subprocess.run call: {cmd!r}")

        monkeypatch.setattr(repo_intel.subprocess, "run", fake_run)
        result = repo_intel.smart_search("AgentLoop traceback", max_files=1)

        assert calls
        assert "pkg/module.py" in result
    finally:
        config.WORKDIR = old_workdir


def test_smart_search_keeps_unmanaged_product_prefixed_sources(
    tmp_path,
    monkeypatch,
):
    """Search must not reserve a prefix created only by local test fixtures."""
    from nz_coder.foundation import config
    from nz_coder.tools import repo_intel

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        source = tmp_path / ".product-catalog" / "catalog.py"
        source.parent.mkdir()
        source.write_text("catalog_needle = 'live'\n", encoding="utf-8")
        monkeypatch.setattr(
            repo_intel.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
        )

        result = repo_intel.smart_search("catalog_needle", max_files=8)

        assert ".product-catalog/catalog.py" in result
    finally:
        config.WORKDIR = old_workdir


def test_diff_status_reports_untracked_non_python_file(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import diff_status

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")

        result = diff_status()

        assert "has_non_empty_diff: true" in result
        assert "pyproject.toml" in result
        assert "diff_chars: 0" not in result
    finally:
        config.WORKDIR = old_workdir


def test_diff_status_non_git_workspace_is_supported(tmp_path):
    """A normal directory must not turn finalization into a Git failure."""
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import diff_status

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        result = diff_status()

        assert not result.startswith("Error:")
        assert "workspace_mode: non_git" in result
        assert "git usage" not in result.lower()
        assert "not a git repository" not in result.lower()
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_checks_untracked_python_file(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import verify_changed_files

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        result = verify_changed_files()

        assert result.startswith("OK: Python syntax parsed in-process")
        assert "OK  app.py" in result
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_checks_tracked_python_file_without_git(tmp_path):
    """Run-local change evidence must support normal non-Git workspaces."""
    from nz_coder.foundation import config
    from nz_coder.state.changes import ChangeTracker
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session, session_change_dir
    from nz_coder.tools.repo_intel import verify_changed_files
    from nz_coder.tools.files import bind_tool_state

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path), scoped_session("non-git-verify"):
            target = tmp_path / "app.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            tracker = ChangeTracker(
                run_id="non-git-change",
                change_dir=session_change_dir(),
            )
            tracker.record_before("app.py", True, "VALUE = 1\n")
            target.write_text("VALUE = 2\n", encoding="utf-8")
            tracker.record_after("app.py", True, "VALUE = 2\n")

            with bind_tool_state(change_tracker=tracker):
                result = verify_changed_files()

        assert result.startswith("OK: Python syntax parsed in-process")
        assert "OK  app.py" in result
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_skips_deleted_python_file(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools import repo_intel

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        monkeypatch.setattr(
            repo_intel,
            "_changed_files_for_verification",
            lambda include_tests: ["deleted.py"],
        )

        result = repo_intel.verify_changed_files()

        assert result.startswith("OK:")
        assert "SKIP deleted.py (deleted file)" in result
    finally:
        config.WORKDIR = old_workdir


def test_smart_search_uses_log_tf_idf_instead_of_linear_line_count(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import smart_search

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "large.py").write_text(
            "\n".join("important_token = None" for _ in range(50)) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "small.py").write_text(
            "def important_token_handler():\n"
            "    return 'important_token'\n",
            encoding="utf-8",
        )

        result = smart_search("important_token", max_files=2)

        assert result.index("small.py") < result.index("large.py")
    finally:
        config.WORKDIR = old_workdir


def test_find_callers_prefers_call_over_attribute_duplicate():
    import ast

    from nz_coder.tools.repo_intel import _find_callers_ast

    tree = ast.parse(
        "obj.foo()\n"
        "foo()\n"
        "value = obj.foo\n"
    )

    refs = _find_callers_ast(tree, "foo", "app.py")

    assert refs == [
        {"file": "app.py", "line": 1, "context": "call: foo(...)"},
        {"file": "app.py", "line": 2, "context": "call: foo(...)"},
        {"file": "app.py", "line": 3, "context": "attr: .foo"},
    ]


def test_read_symbol_lists_and_reads_nested_symbols(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import read_symbol

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "nested.py").write_text(
            "class Outer:\n"
            "    class Inner:\n"
            "        def method(self):\n"
            "            return 1\n"
            "\n"
            "    def method(self):\n"
            "        def helper():\n"
            "            return 2\n"
            "        return helper()\n"
            "\n"
            "def top():\n"
            "    def inner():\n"
            "        return 3\n"
            "    return inner()\n",
            encoding="utf-8",
        )

        listed = read_symbol("nested.py", mode="list")
        selected = read_symbol("nested.py", symbol="Outer.Inner.method", context_lines=0)

        assert "Outer.Inner" in listed
        assert "Outer.Inner.method" in listed
        assert "Outer.method.helper" in listed
        assert "top.inner" in listed
        assert "nested.py:Outer.Inner.method" in selected
        assert "return 1" in selected
    finally:
        config.WORKDIR = old_workdir


def test_smart_search_reuses_ast_parse_for_summary(tmp_path, monkeypatch):
    import subprocess

    from nz_coder.foundation import config
    from nz_coder.tools import repo_intel

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "target.py").write_text(
            "def target_symbol():\n"
            "    return 'target_symbol'\n",
            encoding="utf-8",
        )
        parse_calls = 0
        real_parse = repo_intel.ast.parse

        def fake_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(*args, **kwargs)

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "grep", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, "target.py\n", "")
            raise AssertionError(f"unexpected subprocess.run call: {cmd!r}")

        monkeypatch.setattr(repo_intel.ast, "parse", fake_parse)
        monkeypatch.setattr(repo_intel.subprocess, "run", fake_run)

        result = repo_intel.smart_search("target_symbol", max_files=1)

        assert "target.py" in result
        assert parse_calls == 1
    finally:
        config.WORKDIR = old_workdir


def test_diff_status_recognizes_typescript_tests_without_swebench_warning(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import diff_status

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "src" / "__tests__" / "widget.test.ts"
        target.parent.mkdir(parents=True)
        target.write_text("test(\'ok\', () => {})\n", encoding="utf-8")

        result = diff_status()

        assert "tests_modified: true" in result
        assert "languages_changed: typescript=1" in result
        assert "SWE-bench" not in result
        assert "If the task asks for tests" in result
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_warns_for_typescript_without_configured_checker(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import verify_changed_files

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "src" / "app.ts"
        target.parent.mkdir(parents=True)
        target.write_text("export const value: number = 1\n", encoding="utf-8")

        result = verify_changed_files()

        assert result.startswith("WARN: changed files verification incomplete")
        assert "changed JS/TS files" in result
    finally:
        config.WORKDIR = old_workdir



def test_collect_symbols_respects_max_depth():
    import ast

    from nz_coder.tools.repo_intel import _collect_symbols

    tree = ast.parse(
        "def outer():\n"
        "    def inner():\n"
        "        def too_deep():\n"
        "            return 1\n"
        "        return too_deep()\n"
        "    return inner()\n"
    )

    symbols = _collect_symbols(tree, max_depth=1)

    assert "outer" in symbols
    assert "outer.inner" in symbols
    assert "outer.inner.too_deep" not in symbols


def test_verify_changed_files_suggests_go_compile_without_running_it(tmp_path, monkeypatch):
    import os
    import shlex

    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import verify_changed_files

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
        target = tmp_path / "pkg" / "server.go"
        target.parent.mkdir(parents=True)
        target.write_text("package pkg\nfunc Value() int { return 1 }\n", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        args_path = tmp_path / "go_args.txt"
        fake_go = bin_dir / "go"
        fake_go.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(args_path))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_go.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

        result = verify_changed_files()

        assert result.startswith("WARN: changed files verification incomplete")
        assert "NOT EXECUTED go compile ./pkg: go test ./pkg -run" in result
        assert "bash/process" in result
        assert not args_path.exists()
        assert result.execution_facts == ()
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_warns_for_go_without_module_metadata(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools import repo_intel

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "pkg" / "server.go"
        target.parent.mkdir(parents=True)
        target.write_text("package pkg\n", encoding="utf-8")
        result = repo_intel.verify_changed_files()

        assert result.startswith("WARN: changed files verification incomplete")
        assert "no root go.mod or go.work" in result
    finally:
        config.WORKDIR = old_workdir


def test_node_typecheck_command_prefers_pnpm_lockfile(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import _node_typecheck_command

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "package.json").write_text(
            '{"scripts":{"typecheck":"tsc --noEmit"}}',
            encoding="utf-8",
        )
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")

        assert _node_typecheck_command() == ["pnpm", "run", "typecheck"]
    finally:
        config.WORKDIR = old_workdir


def test_node_typecheck_command_prefers_yarn_lockfile(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import _node_typecheck_command

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "package.json").write_text(
            '{"scripts":{"typecheck":"tsc --noEmit"}}',
            encoding="utf-8",
        )
        (tmp_path / "yarn.lock").write_text("# yarn lockfile\n", encoding="utf-8")

        assert _node_typecheck_command() == ["yarn", "typecheck"]
    finally:
        config.WORKDIR = old_workdir


def test_read_symbol_respects_max_depth_parameter(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import read_symbol

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "nested.py").write_text(
            "class Outer:\n"
            "    class Inner:\n"
            "        def method(self):\n"
            "            return 1\n"
            "\n"
            "    def method(self):\n"
            "        def helper():\n"
            "            return 2\n"
            "        return helper()\n",
            encoding="utf-8",
        )

        listed = read_symbol("nested.py", mode="list", max_depth=1)

        assert "Outer" in listed
        assert "Outer.Inner" in listed
        assert "Outer.Inner.method" not in listed
        assert "Outer.method.helper" not in listed
    finally:
        config.WORKDIR = old_workdir


def test_smart_search_no_files_message_is_include_aware(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_intel import smart_search

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        result = smart_search("widget token", include="*.ts")

        assert "No files matching '*.ts' found under '.'" in result
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_denied_bash_never_runs_project_checks(tmp_path, monkeypatch):
    import json
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.command_channel import scoped_command_client
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.checker import PermissionChecker
    from nz_coder.tool_platform.permissioning.rules import parse_rules
    from nz_coder.tools import repo_intel

    class Permissions:
        def __init__(self):
            self.checker = PermissionChecker("default", workspace_trusted=False)
            self.denies = parse_rules(["Bash"], "deny")

        def check(self, name, arguments):
            return self.checker.check(name, arguments, [], self.denies, [])

        def ask_user(self, *args):
            raise AssertionError("read-only verification should not ask")

    class DenyingParent:
        def call(self, *args, **kwargs):
            raise AssertionError("verification must only suggest external commands")

    changed = ["app.py", "app.ts", "pkg/app.go", "src/main.rs"]
    for name, source in zip(changed, ["VALUE = 1", "export const x = 1", "package pkg", "fn main() {}"]):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(source)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"typecheck": "node sentinel.js"}}))
    (tmp_path / "go.mod").write_text("module example.invalid/demo")
    (tmp_path / "Cargo.toml").write_text('[package]\nname="demo"\nversion="0.1.0"')
    sentinel = tmp_path / "PROJECT_CHECK_RAN"
    calls = []

    def safe_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] != "git":
            sentinel.write_text("unexpected project check")
        output = "true\n" if argv[:2] == ["git", "rev-parse"] else "\n".join(changed) if argv[:2] == ["git", "diff"] else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    permissions = Permissions()
    monkeypatch.setattr(repo_intel.subprocess, "run", safe_run)
    with scoped_workdir(tmp_path), scoped_command_client(DenyingParent()):
        assert permissions.check("bash", {"command": "npm run -s typecheck"})["behavior"] == "deny"
        result = ToolExecutor(permissions).execute_one({"id": "verify-boundary", "function": {
            "name": "verify_changed_files", "arguments": {}}}, 0)
    assert result.executed and not result.dispatch_failed
    assert not sentinel.exists(), calls
    assert all(argv[0] == "git" for argv in calls)
    assert result.execution_facts == ()
    assert "not executed" in result.output.lower()
    assert "bash/process" in result.output


def test_verify_changed_files_python_parses_in_process_without_command_receipt(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.changes import ChangeTracker
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.tools import repo_intel

    source = 'raise RuntimeError("source must not run")\n'
    (tmp_path / "app.py").write_text(source)
    tracker = ChangeTracker(change_dir=tmp_path / "owner")
    tracker.record_before("app.py", False)
    tracker.record_after("app.py", True, source)
    monkeypatch.setattr(repo_intel, "_run_git", lambda _args: subprocess.CompletedProcess([], 1, "", ""))

    def reject_process(*args, **kwargs):
        raise AssertionError("language verification must stay in this process")

    monkeypatch.setattr(repo_intel.subprocess, "run", reject_process)
    with scoped_workdir(tmp_path), bind_tool_state(change_tracker=tracker):
        result = repo_intel.verify_changed_files()
        assert str(result).startswith("OK: Python syntax"), result
        assert result.execution_facts == ()
        assert not (tmp_path / "__pycache__").exists()
        (tmp_path / "app.py").write_text("def broken(:\n")
        result = repo_intel.verify_changed_files()
        assert str(result).startswith("FAIL:"), result
        assert "app.py" in result
        assert result.execution_facts == ()


@pytest.mark.parametrize(
    ("encoding", "source"),
    [
        ("utf-8-sig", "label = 'café'\n"),
        ("latin-1", "# coding: latin-1\nlabel = 'café'\n"),
    ],
    ids=["utf8-bom", "coding-latin1"],
)
def test_verify_changed_files_accepts_python_source_encoding(
    tmp_path, monkeypatch, encoding, source,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import repo_intel

    sentinel = tmp_path / "SOURCE_EXECUTED"
    source += f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
    (tmp_path / "encoded.py").write_bytes(source.encode(encoding))
    (tmp_path / "plain.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    def git_query(args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            output = "true\n"
        elif args == ["diff", "--name-only", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"]:
            output = "encoded.py\nplain.py\nbroken.py\n"
        elif args == ["ls-files", "--others", "--exclude-standard"]:
            output = ""
        else:
            raise AssertionError(f"unexpected Git query: {args!r}")
        return subprocess.CompletedProcess(["git", *args], 0, output, "")

    monkeypatch.setattr(repo_intel, "_run_git", git_query)
    with scoped_workdir(tmp_path):
        result = repo_intel.verify_changed_files()

    assert "OK  encoded.py" in result, result
    assert "OK  plain.py" in result, result
    assert str(result).startswith("FAIL:"), result
    assert "FAIL broken.py" in result, result
    assert result.execution_facts == ()
    assert not sentinel.exists()
    assert not (tmp_path / "__pycache__").exists()
