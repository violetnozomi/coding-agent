"""Source-level Skill tool content and cooperative cancellation contracts."""
from __future__ import annotations

import shutil
import threading

from nz_coder.state.skills import SkillLoader
from nz_coder.tools import ToolOutput, scoped_tool_cancellation


def _loader(tmp_path, *, resources: int = 0):
    project = tmp_path / "project"
    directory = project / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n"
        "allowed_tools: read_file, grep_search\n---\n"
        "Read the requested files carefully.",
        encoding="utf-8",
    )
    for index in range(resources):
        (directory / f"resource-{index:02d}.md").write_text(
            str(index),
            encoding="utf-8",
        )
    return SkillLoader(
        bundled_dir=tmp_path / "missing-bundled",
        user_dir=tmp_path / "missing-user",
        project_dir=project,
    ), directory


def test_skill_load_returns_content_base_files_and_metadata(tmp_path):
    loader, directory = _loader(tmp_path, resources=12)

    result = loader.load("review")

    assert isinstance(result, ToolOutput)
    assert result.title == "Loaded skill: review"
    assert result.metadata == {"name": "review", "dir": str(directory.resolve())}
    assert '<skill_content name="review" source="project">' in result
    assert "# Skill: review" in result
    assert "Read the requested files carefully." in result
    assert f"Base directory for this skill: {directory.resolve().as_uri()}" in result
    assert "<!-- allowed_tools: read_file, grep_search -->" in result
    assert result.count("<file>") == 10
    assert "SKILL.md</file>" not in result


def test_skill_file_sampling_observes_tool_cancel(tmp_path, monkeypatch):
    loader, directory = _loader(tmp_path, resources=2)
    first = directory / "resource-00.md"
    second = directory / "resource-01.md"
    yielded = threading.Event()
    release = threading.Event()
    def slow_files(path, **_kwargs):
        assert path == directory.resolve()
        yield first
        yielded.set()
        release.wait(1)
        yield second

    monkeypatch.setattr("nz_coder.runtime.ripgrep.shutil.which", lambda _name: None)
    monkeypatch.setattr("nz_coder.runtime.ripgrep._iter_fallback_paths", slow_files)
    cancel_event = threading.Event()
    result = []

    def worker():
        with scoped_tool_cancellation(cancel_event):
            result.append(loader.load("review"))

    thread = threading.Thread(target=worker)
    thread.start()
    assert yielded.wait(1)
    cancel_event.set()
    release.set()
    thread.join(1)

    assert thread.is_alive() is False
    assert result == ["Error: Skill loading cancelled"]


def test_unknown_skill_keeps_error_contract(tmp_path):
    loader, _directory = _loader(tmp_path)

    result = loader.load("missing")

    assert result == "Error: Unknown skill 'missing'. Available: review"


def test_skill_real_rg_includes_hidden_but_honors_ignore_without_user_glob(tmp_path):
    if shutil.which("rg") is None:
        return
    loader, directory = _loader(tmp_path)
    (directory / ".hidden.md").write_text("hidden", encoding="utf-8")
    (directory / "ignored.md").write_text("ignored", encoding="utf-8")
    (directory / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    git_dir = directory / ".git"
    git_dir.mkdir()
    (git_dir / "internal.md").write_text("internal", encoding="utf-8")

    result = loader.load("review")

    assert str(directory / ".hidden.md") in result
    assert str(directory / "ignored.md") not in result
    assert str(git_dir / "internal.md") not in result
