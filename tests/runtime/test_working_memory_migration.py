"""仅在显式副本上验收离线格式搬迁，不调用模型。"""
import json

import pytest

from scripts.migrate_working_memory import migrate_file
from nz_coder.tools.scratchpad import Scratchpad


def old_payload():
    return {'version': 1, 'entries': [
        {'category': 'plan', 'content': '完整原文 ' * 3000, 'ts': 123.5},
        {'category': 'failure', 'content': '未经证实的原因，不改变措辞', 'ts': 456.5},
        {'category': 'finding', 'content': '历史别名原文', 'ts': 789.5}],
        'reminder': {'revision': 7, 'completed_steps': 50,
                     'last_check_step': 50, 'pressure_notified': True}}


def test_dry_run_then_apply_preserves_original_and_metadata(tmp_path):
    path = tmp_path / 'notes.json'
    old = old_payload()
    raw = json.dumps(old, ensure_ascii=False).encode()
    path.write_bytes(raw)
    assert migrate_file(path)['status'] == 'dry_run'
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]
    result = migrate_file(path, apply=True)
    assert result['status'] == 'migrated'
    assert open(result['backup'], 'rb').read() == raw
    migrated = json.loads(path.read_text())
    assert migrated['reminder'] == old['reminder']
    pad = Scratchpad(path)
    assert pad.memory['focus'][0]['summary'] == old['entries'][0]['content']
    assert pad.memory['focus'][1]['summary'] == old['entries'][2]['content']
    assert pad.memory['lessons'][0]['summary'] == old['entries'][1]['content']
    assert pad.memory['lessons'][0]['ts'] == 456.5
    assert pad.memory['findings'] == pad.memory['decisions'] == []
    assert migrate_file(path, apply=True)['status'] == 'already_current'


@pytest.mark.parametrize('bad', [None, {'version': 3}, {'version': 1, 'entries': [
    {'category': 'unknown', 'content': 'keep', 'ts': 1}]}])
def test_invalid_file_keeps_bytes(tmp_path, bad):
    path = tmp_path / 'notes.json'
    raw = json.dumps(bad).encode()
    path.write_bytes(raw)
    assert migrate_file(path, apply=True)['status'] == 'error'
    assert path.read_bytes() == raw


def test_changed_file_after_backup_is_not_overwritten(monkeypatch, tmp_path):
    from scripts import migrate_working_memory as migration
    path = tmp_path / 'notes.json'
    path.write_text(json.dumps(old_payload()))
    backup = migration._backup
    def changed(path, raw):
        result = backup(path, raw)
        path.write_text('CHANGED_EXTERNALLY')
        return result
    monkeypatch.setattr(migration, '_backup', changed)
    result = migrate_file(path, apply=True)
    assert result['status'] == 'error'
    assert path.read_text() == 'CHANGED_EXTERNALLY'


def test_backup_failure_never_overwrites(monkeypatch, tmp_path):
    from scripts import migrate_working_memory as migration
    path = tmp_path / 'notes.json'
    path.write_text(json.dumps(old_payload()))
    before = path.read_bytes()
    def fail(*_):
        raise OSError('disk full')
    monkeypatch.setattr(migration, '_backup', fail)
    assert migrate_file(path, apply=True)['status'] == 'error'
    assert path.read_bytes() == before


@pytest.mark.parametrize('name', ['002', '003'])
def test_real_long_task_note_copy_preserves_every_character_and_timestamp(tmp_path, name):
    from pathlib import Path
    source = Path(__file__).parent / 'fixtures' / f'working_memory_long_task_v1_{name}.json'
    original = source.read_bytes()
    old = json.loads(original)
    target = tmp_path / source.name
    target.write_bytes(original)
    assert migrate_file(target, apply=True)['status'] == 'migrated'
    migrated = json.loads(target.read_bytes())
    assert migrated['reminder'] == old['reminder']
    for category, section in [('plan', 'focus'), ('failure', 'lessons')]:
        before = [e for e in old['entries'] if e['category'] == category]
        after = migrated['memory'][section]
        assert [(e['summary'], e['ts'], e['details']) for e in after] == [
            (e['content'], e['ts'], '') for e in before]
    view = Scratchpad(target).build_prompt_block()
    assert all(entry['content'] in view for entry in old['entries'])
    assert source.read_bytes() == original


def test_atomic_write_failure_keeps_old_file_and_backup(monkeypatch, tmp_path):
    from scripts import migrate_working_memory as migration
    path = tmp_path / 'notes.json'
    path.write_text(json.dumps(old_payload()))
    raw = path.read_bytes()
    def fail(*_):
        raise OSError('write failed')
    monkeypatch.setattr(migration, 'write_session_runtime_json', fail)
    result = migrate_file(path, apply=True)
    assert result['status'] == 'error'
    assert path.read_bytes() == raw
    assert open(result['backup'], 'rb').read() == raw
