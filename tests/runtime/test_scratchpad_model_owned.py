"""模型整理笔记和每消息提醒节奏的行为回归。"""
from __future__ import annotations

import json

from nz_coder.tools.scratchpad import Scratchpad, _update_scratchpad, scoped_scratchpad, empty_memory


def test_model_replaces_one_category_and_clears_it_without_empty_entries(tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    pad.update_section('focus', [{"summary": "OUTDATED_FINDING", "details": ""}])
    pad.update_section('lessons', [{"summary": "KEEP_FAILURE_REASON", "details": ""}])
    with scoped_scratchpad(pad):
        result = _update_scratchpad(section="focus", entries=[{"summary": "CURRENT_FINDING", "details": ""}], mode="replace")
    assert not str(result).startswith("Error:")
    assert "OUTDATED_FINDING" not in pad.read()
    assert "CURRENT_FINDING" in pad.read()
    assert "KEEP_FAILURE_REASON" in pad.read()
    with scoped_scratchpad(pad):
        result = _update_scratchpad(section="focus", entries=[], mode="replace")
    assert not str(result).startswith("Error:")
    assert pad.memory == Scratchpad(path).memory
    assert pad.memory["focus"] == []
    assert len(pad.memory["lessons"]) == 1


def test_replace_and_handoff_are_rejected_without_writing(tmp_path):
    pad = Scratchpad(tmp_path / "notes.json")
    pad.update_section('focus', [{"summary": "KEEP", "details": ""}])
    before = pad.read()
    with scoped_scratchpad(pad):
        result = _update_scratchpad("plan", "PROPOSAL", next_action={"kind": "inspect"}, mode="replace")
    assert str(result).startswith("Error:")
    assert pad.read() == before


def test_failed_replace_preserves_disk_and_memory(monkeypatch, tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    pad.update_section('focus', [{"summary": "KEEP", "details": ""}])
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("nz_coder.state.sessions.write_session_runtime_json", fail)
    with scoped_scratchpad(pad):
        result = _update_scratchpad(section="focus", entries=[{"summary": "REPLACEMENT", "details": ""}], mode="replace")
    assert str(result).startswith("Error:")
    assert "KEEP" in pad.read() and "REPLACEMENT" not in pad.read()
    assert path.read_bytes() == before


def test_empty_memory_reminder_after_25_completed_steps_and_resume(tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    for _ in range(24):
        assert pad.complete_step(pad.revision) is None
    assert pad.reminder_reason(25, False) == ""
    assert pad.complete_step(pad.revision) is None
    assert pad.read() == "Scratchpad is empty."
    assert pad.reminder_reason(25, False) == "periodic"
    resumed = Scratchpad(path)
    assert resumed.reminder_reason(25, False) == "periodic"
    for _ in range(3):
        assert resumed.reminder_reason(25, False) == "periodic"
    assert resumed.begin_maintenance(under_pressure=False) is None
    assert resumed.reminder_reason(25, False) == ""
    assert json.loads(path.read_text())["memory"] == empty_memory()


def test_note_written_inside_current_step_resets_age_after_settlement(tmp_path):
    pad = Scratchpad(tmp_path / "notes.json")
    for _ in range(24):
        pad.complete_step(pad.revision)
    before = pad.revision
    pad.update_section('focus', [{"summary": "CONFIRMED_IN_THIS_STEP", "details": ""}])
    assert pad.complete_step(before) is None
    assert pad.reminder_reason(25, False) == ""
    for _ in range(24):
        pad.complete_step(pad.revision)
    assert pad.reminder_reason(25, False) == ""
    pad.complete_step(pad.revision)
    assert pad.reminder_reason(25, False) == "periodic"


def test_pressure_notice_is_one_per_episode_and_zero_disables_all(tmp_path):
    pad = Scratchpad(tmp_path / "notes.json")
    assert pad.reminder_reason(0, True) == ""
    assert pad.reminder_reason(25, True) == "pressure"
    pad.begin_maintenance(under_pressure=True)
    assert pad.reminder_reason(25, True) == ""
    pad.complete_step(pad.revision, under_pressure=False)
    assert pad.reminder_reason(25, True) == "pressure"


def test_handoff_does_not_advance_note_revision_or_satisfy_reminder(tmp_path):
    pad = Scratchpad(tmp_path / "notes.json")
    for _ in range(25):
        pad.complete_step(pad.revision)
    before = pad.revision
    with scoped_scratchpad(pad):
        result = _update_scratchpad("plan", "HYPOTHESIS", next_action={"kind": "inspect"})
    assert "proposed" in str(result)
    assert pad.revision == before
    assert pad.reminder_reason(25, False) == "periodic"
    assert pad.read() == "Scratchpad is empty."


def test_failed_schedule_write_keeps_prior_schedule(monkeypatch, tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    for _ in range(25):
        pad.complete_step(pad.revision)
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("nz_coder.state.sessions.write_session_runtime_json", fail)
    assert pad.begin_maintenance(under_pressure=False)
    assert path.read_bytes() == before
    assert pad.reminder_reason(25, False) == "periodic"


def test_whole_model_replacement_is_atomic_and_revision_bound(tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    pad.update_section('focus', [{"summary": "old", "details": ""}])
    revision = pad.revision
    notes = empty_memory() | {"focus": [{"summary": "fact", "details": ""}],
                              "lessons": [{"summary": "lesson", "details": ""}]}
    assert pad.replace_memory(notes, expected_revision=revision) is None
    restored = Scratchpad(path).memory
    assert restored["focus"][0]["summary"] == "fact"
    assert restored["lessons"][0]["summary"] == "lesson"
    assert pad.replace_memory(empty_memory(), expected_revision=revision)
    assert "fact" in pad.read()
    assert pad.replace_memory(empty_memory(), expected_revision=pad.revision) is None
    assert Scratchpad(path).memory == empty_memory()


def test_failed_result_save_keeps_prior_notes_and_attempt_anchor(monkeypatch, tmp_path):
    path = tmp_path / "notes.json"
    pad = Scratchpad(path)
    pad.update_section('focus', [{"summary": "keep", "details": ""}])
    for _ in range(25):
        pad.complete_step(pad.revision)
    assert pad.begin_maintenance(under_pressure=True) is None
    revision = pad.revision

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("nz_coder.state.sessions.write_session_runtime_json", fail)
    assert pad.replace_memory(empty_memory() | {"focus": [{"summary": "new", "details": ""}]}, expected_revision=revision)
    assert pad.revision == revision
    assert "keep" in Scratchpad(path).read()
    assert pad.reminder_reason(25, True) == ""
