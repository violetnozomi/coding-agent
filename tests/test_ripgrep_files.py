"""Tests for the shared InfCode-style Ripgrep.files runtime producer."""
from __future__ import annotations

import json
import subprocess

import pytest

from nz_coder.capabilities.ripgrep import (
    decode_ripgrep_event,
    list_ripgrep_files,
    search_ripgrep,
)
from nz_coder.state import skills as skills_module
from nz_coder.tools import search as search_module


def _fake_rg(tmp_path, rows: list[str], *, marker=None, code: int = 0):
    script = tmp_path / f"fake-files-rg-{len(rows)}-{code}"
    lines = ["#!/usr/bin/env python3", "import json, os, sys"]
    if marker is not None:
        lines.append(
            f"open({str(marker)!r}, 'w', encoding='utf-8').write("
            "json.dumps({'argv': sys.argv[1:], "
            "'config': os.environ.get('RIPGREP_CONFIG_PATH')}))"
        )
    lines.extend(f"print({row!r}, flush=True)" for row in rows)
    lines.append(f"sys.exit({code})")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _stats() -> dict:
    return {
        "elapsed": {"secs": 0, "nanos": 1, "human": "0.000001s"},
        "searches": 1,
        "searches_with_match": 1,
        "bytes_searched": 6,
        "bytes_printed": 10,
        "matched_lines": 1,
        "matches": 1,
    }


def _match(path: str = "source.py") -> dict:
    return {
        "type": "match",
        "data": {
            "path": {"text": path},
            "lines": {"text": "needle\n"},
            "line_number": 1,
            "absolute_offset": 0,
            "submatches": [{
                "match": {"text": "needle"},
                "start": 0,
                "end": 6,
            }],
        },
    }


def test_shared_files_filters_before_limit_and_detects_truncation(tmp_path, monkeypatch):
    fake = _fake_rg(
        tmp_path,
        [
            "SKILL.md",
            "nested/NOT-SKILL.md.backup",
            "one.txt",
            "two.txt",
            "three.txt",
        ],
    )
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake))

    result = list_ripgrep_files(
        tmp_path,
        hidden=True,
        follow=False,
        limit=2,
        exclude=lambda path: "SKILL.md" in path,
    )

    assert result.files == ("one.txt", "two.txt")
    assert result.truncated is True
    assert result.used_ripgrep is True


def test_shared_files_without_user_glob_preserves_core_args_and_clean_env(
    tmp_path,
    monkeypatch,
):
    marker = tmp_path / "invocation.json"
    fake = _fake_rg(tmp_path, ["resource.txt"], marker=marker)
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake))
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/tmp/untrusted-rg-config")

    result = list_ripgrep_files(
        tmp_path,
        hidden=True,
        follow=False,
        limit=10,
    )
    invocation = json.loads(marker.read_text(encoding="utf-8"))

    assert result.files == ("resource.txt",)
    assert invocation["argv"] == [
        "--no-config",
        "--files",
        "--glob=!.git/*",
        "--hidden",
        ".",
    ]
    assert invocation["config"] is None


def test_shared_fallback_honors_hidden_depth_and_follow_false(tmp_path, monkeypatch):
    (tmp_path / "root.txt").write_text("", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.txt").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(outside)
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: None)

    result = list_ripgrep_files(
        tmp_path,
        hidden=False,
        follow=False,
        max_depth=0,
        limit=20,
    )

    assert result.files == ("root.txt", "outside.txt")
    assert result.used_ripgrep is False
    assert ".hidden/secret.txt" not in result.files
    assert "nested/deep.txt" not in result.files
    assert "linked.txt" not in result.files


def test_glob_and_skill_reference_the_same_runtime_producer():
    assert search_module.list_ripgrep_files is list_ripgrep_files
    assert search_module.search_ripgrep is search_ripgrep
    assert skills_module.list_ripgrep_files is list_ripgrep_files


def test_shared_real_rg_without_positive_glob_honors_ignore_and_git(tmp_path):
    import shutil

    if shutil.which("rg") is None:
        return
    (tmp_path / ".hidden.txt").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "internal.txt").write_text("", encoding="utf-8")

    result = list_ripgrep_files(tmp_path, hidden=True, follow=False, limit=20)

    assert ".hidden.txt" in result.files
    assert "ignored.txt" not in result.files
    assert ".git/internal.txt" not in result.files


def test_shared_search_preserves_args_clean_env_and_typed_result(tmp_path, monkeypatch):
    marker = tmp_path / "search-invocation.json"
    fake = _fake_rg(tmp_path, [json.dumps(_match())], marker=marker, code=2)
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake))
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/tmp/untrusted-rg-config")

    result = search_ripgrep(
        tmp_path,
        "needle",
        patterns=("*.py",),
        limit=3,
        follow=True,
        files=("source.py",),
        case_insensitive=True,
    )
    invocation = json.loads(marker.read_text(encoding="utf-8"))

    assert invocation["argv"] == [
        "--no-config",
        "--json",
        "--hidden",
        "--glob=!.git/*",
        "--no-messages",
        "--follow",
        "--glob=*.py",
        "--max-count=3",
        "--ignore-case",
        "--",
        "needle",
        "source.py",
    ]
    assert invocation["config"] is None
    assert result.partial is True
    assert len(result.items) == 1
    assert result.items[0].path == "source.py"


def test_shared_search_code_one_discards_emitted_match_rows(tmp_path, monkeypatch):
    fake = _fake_rg(tmp_path, [json.dumps(_match())], code=1)
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake))

    result = search_ripgrep(tmp_path, "needle")

    assert result.items == ()
    assert result.partial is False


def test_decode_validates_full_non_match_event_union():
    stats = _stats()
    events = [
        {"type": "begin", "data": {"path": {"text": "source.py"}}},
        {
            "type": "end",
            "data": {
                "path": {"text": "source.py"},
                "binary_offset": None,
                "stats": stats,
            },
        },
        {
            "type": "summary",
            "data": {
                "elapsed_total": {"secs": 0, "nanos": 2, "human": "0.000002s"},
                "stats": stats,
            },
        },
    ]

    assert [decode_ripgrep_event(json.dumps(event)) for event in events] == [
        None,
        None,
        None,
    ]

    broken = events[-1].copy()
    broken["data"] = dict(broken["data"])
    broken["data"]["stats"] = dict(stats, matches=-1)
    with pytest.raises(ValueError, match="summary stats matches"):
        decode_ripgrep_event(json.dumps(broken))


def test_shared_search_timeout_terminates_and_settles_process(tmp_path, monkeypatch):
    fake = tmp_path / "slow-rg"
    fake.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    processes = []
    real_popen = subprocess.Popen

    def observed_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("nz_coder.capabilities.ripgrep.shutil.which", lambda _name: str(fake))
    monkeypatch.setattr("nz_coder.capabilities.ripgrep.subprocess.Popen", observed_popen)

    with pytest.raises(subprocess.TimeoutExpired):
        search_ripgrep(tmp_path, "needle", timeout=0.05)

    assert len(processes) == 1
    assert processes[0].poll() is not None
