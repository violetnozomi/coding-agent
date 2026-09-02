"""InfCode-style cooperative cancellation for grep and glob search tools."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import ToolOutput, scoped_tool_cancellation
from nz_coder.tools.search import (
    _SearchInterrupted,
    _run_rg_files,
    _run_rg_search,
    grep_search,
    glob_search,
)


def test_grep_subprocess_is_terminated_on_tool_cancel(tmp_path, monkeypatch):
    fake_rg = tmp_path / "fake-rg"
    fake_rg.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "print(json.dumps({'type': 'begin', 'data': {'path': {'text': 'source.py'}}}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    started = threading.Event()
    process_holder = []
    real_popen = subprocess.Popen

    def observed_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        process_holder.append(process)
        started.set()
        return process

    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake_rg))
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.subprocess.Popen", observed_popen)
    cancel_event = threading.Event()
    interrupted = []

    def worker():
        with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel_event):
            try:
                _run_rg_search(tmp_path, "needle")
            except _SearchInterrupted:
                interrupted.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(1)
    cancel_event.set()
    thread.join(1)

    assert thread.is_alive() is False
    assert interrupted == [True]
    assert process_holder[0].poll() is not None


def test_glob_scan_observes_tool_cancel_between_paths(tmp_path, monkeypatch):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    yielded = threading.Event()
    release = threading.Event()
    def slow_files(path, **_kwargs):
        assert path == tmp_path.resolve()
        yield first
        yielded.set()
        release.wait(1)
        yield second

    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)
    monkeypatch.setattr("nz_coder.capabilities.ripgrep._iter_fallback_paths", slow_files)
    cancel_event = threading.Event()
    result = []

    def worker():
        with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel_event):
            result.append(glob_search("**/*.py"))

    thread = threading.Thread(target=worker)
    thread.start()
    assert yielded.wait(1)
    cancel_event.set()
    release.set()
    thread.join(1)

    assert thread.is_alive() is False
    assert result == ["Error: Search cancelled"]


def test_python_grep_fallback_returns_error_when_cancelled(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("needle\n", encoding="utf-8")
    cancel_event = threading.Event()

    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)
    cancel_event.set()
    with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel_event):
        result = grep_search("needle")

    assert result == "Error: Search cancelled"


def test_glob_double_star_matches_root_and_nested_files(tmp_path):
    (tmp_path / "root.py").write_text("", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "nested.py").write_text("", encoding="utf-8")

    with scoped_workdir(tmp_path):
        result = glob_search("**/*.py")

    assert "root.py" in result
    assert "src/nested.py" in result


def test_glob_basename_patterns_recurse_like_ripgrep_globset(tmp_path):
    (tmp_path / "root.py").write_text("", encoding="utf-8")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    (nested / "nested.py").write_text("", encoding="utf-8")

    with scoped_workdir(tmp_path):
        basename = glob_search("*.py")
        recursive = glob_search("src/**")

    assert "root.py" in basename
    assert "src/deep/nested.py" in basename
    assert "src/deep/nested.py" in recursive


def test_glob_defaults_exclude_managed_state_and_repository_noise(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
    for directory in (
        ".nz-coder", ".git", ".pytest_cache", "node_modules",
    ):
        target = tmp_path / directory
        target.mkdir()
        (target / "private.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        result = glob_search("*")

    assert "src/app.py" in result
    assert ".nz-coder" not in result
    assert ".git" not in result
    assert ".pytest_cache" not in result
    assert "node_modules" not in result
def test_glob_explicit_private_scope_cannot_inspect_product_state(tmp_path, monkeypatch):
    private = tmp_path / ".nz-coder"
    private.mkdir()
    (private / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        result = glob_search("*.jsonl", ".nz-coder")

    assert result.startswith("Error: Model access blocked")


def test_glob_keeps_unmanaged_product_prefixed_directories(tmp_path, monkeypatch):
    """Only NZ-Coder-managed paths are private by default."""
    source = tmp_path / ".product-catalog"
    source.mkdir()
    (source / "catalog.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        result = glob_search("*")

    assert ".product-catalog/catalog.py" in result


def test_glob_returns_only_absolute_files_sorted_by_mtime_with_metadata(
    tmp_path,
    monkeypatch,
):
    old = tmp_path / "old.py"
    newest = tmp_path / "src" / "new.py"
    directory = tmp_path / "empty.py"
    newest.parent.mkdir()
    directory.mkdir()
    old.write_text("", encoding="utf-8")
    newest.write_text("", encoding="utf-8")
    os.utime(old, (10, 10))
    os.utime(newest, (20, 20))
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        result = glob_search("*.py")

    assert isinstance(result, ToolOutput)
    assert result.splitlines() == [str(newest), str(old)]
    assert str(directory) not in result
    assert result.title == ""
    assert result.metadata == {"count": 2, "truncated": False}


def test_glob_path_scope_braces_and_empty_result_match_infcode_shape(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "src"
    source.mkdir()
    python_file = source / "main.py"
    text_file = source / "notes.txt"
    python_file.write_text("", encoding="utf-8")
    text_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        selected = glob_search("*.{py,txt}", "src")
        missing = glob_search("*.rs", "src")

    assert set(selected.splitlines()) == {str(python_file), str(text_file)}
    assert selected.title == "src"
    assert selected.metadata == {"count": 2, "truncated": False}
    assert str(missing) == "No files found"
    assert missing.metadata == {"count": 0, "truncated": False}


def test_glob_truncates_first_producer_window_then_sorts_it(tmp_path, monkeypatch):
    selected = []
    for index in range(100):
        path = tmp_path / f"file-{index:03d}.py"
        path.write_text("", encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
        selected.append(path.name)
    omitted = tmp_path / "omitted.py"
    omitted.write_text("", encoding="utf-8")
    os.utime(omitted, (10_000, 10_000))
    monkeypatch.setattr(
        "nz_coder.tools.search._run_rg_files",
        lambda _base, _pattern, _limit: (selected, True),
    )

    with scoped_workdir(tmp_path):
        result = glob_search("*.py")

    assert result.splitlines()[0] == str(tmp_path / "file-099.py")
    assert str(omitted) not in result
    assert "Results are truncated: showing first 100 results" in result
    assert result.metadata == {"count": 100, "truncated": True}


def test_glob_absolute_pattern_stays_inside_workspace(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    target = source / "main.py"
    target.write_text("", encoding="utf-8")

    with scoped_workdir(tmp_path):
        inside = glob_search(str(source / "*.py"))
        outside = glob_search("/tmp/*.py")
        file_base = glob_search("*", "src/main.py")

    assert str(target) in inside
    assert outside.startswith("Error: Path escapes workspace")
    assert file_base.startswith("Error: glob path must be a directory")


def test_glob_ripgrep_process_is_terminated_on_tool_cancel(tmp_path, monkeypatch):
    fake_rg = tmp_path / "fake-rg"
    fake_rg.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "print('first.py', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    (tmp_path / "first.py").write_text("", encoding="utf-8")
    started = threading.Event()
    real_popen = subprocess.Popen

    def observed_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake_rg))
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.subprocess.Popen", observed_popen)
    cancel_event = threading.Event()
    result = []

    def worker():
        with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel_event):
            result.append(glob_search("*.py"))

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(1)
    cancel_event.set()
    thread.join(2)

    assert thread.is_alive() is False
    assert result == ["Error: Search cancelled"]


def test_glob_system_ripgrep_keeps_repository_metadata_private_by_default(tmp_path):
    if shutil.which("rg") is None:
        return
    (tmp_path / ".hidden.py").write_text("", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "internal.py").write_text("", encoding="utf-8")

    with scoped_workdir(tmp_path):
        files, truncated = _run_rg_files(tmp_path, "*.py", 100)

    assert ".hidden.py" in files
    assert "ignored.py" in files
    assert ".git/internal.py" not in files
    assert truncated is False
