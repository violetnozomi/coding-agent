"""Tests for repository intelligence tools."""
from __future__ import annotations

import subprocess


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_smart_search_uses_valid_git_grep_pathspec(tmp_path, monkeypatch):
    from nz_coder import config
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


def test_diff_status_reports_untracked_non_python_file(tmp_path):
    from nz_coder import config
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


def test_verify_changed_files_checks_untracked_python_file(tmp_path):
    from nz_coder import config
    from nz_coder.tools.repo_intel import verify_changed_files

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        result = verify_changed_files()

        assert result.startswith("OK: py_compile changed files")
        assert "OK  app.py" in result
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_skips_deleted_python_file(tmp_path, monkeypatch):
    from nz_coder import config
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
    from nz_coder import config
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
    from nz_coder import config
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

    from nz_coder import config
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
    from nz_coder import config
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
    from nz_coder import config
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


def test_verify_changed_files_go_compile_does_not_run_tests(tmp_path, monkeypatch):
    import os
    import shlex

    from nz_coder import config
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

        assert result.startswith("OK: changed files verification")
        assert args_path.read_text(encoding="utf-8").splitlines() == ["test", "./pkg", "-run", "^$"]
    finally:
        config.WORKDIR = old_workdir


def test_verify_changed_files_warns_for_go_without_module_metadata(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.tools import repo_intel

    _init_repo(tmp_path)
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "pkg" / "server.go"
        target.parent.mkdir(parents=True)
        target.write_text("package pkg\n", encoding="utf-8")
        monkeypatch.setattr(
            repo_intel,
            "_run_verifier",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
        )

        result = repo_intel.verify_changed_files()

        assert result.startswith("WARN: changed files verification incomplete")
        assert "no root go.mod or go.work" in result
    finally:
        config.WORKDIR = old_workdir


def test_node_typecheck_command_prefers_pnpm_lockfile(tmp_path):
    from nz_coder import config
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
    from nz_coder import config
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
    from nz_coder import config
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
    from nz_coder import config
    from nz_coder.tools.repo_intel import smart_search

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        result = smart_search("widget token", include="*.ts")

        assert "No files matching '*.ts' found under '.'" in result
    finally:
        config.WORKDIR = old_workdir
