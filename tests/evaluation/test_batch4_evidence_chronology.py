"""实际导出入口必须按第四批真实阶段顺序合并，不能覆盖已付费尝试。"""
from pathlib import Path
import json
import runpy
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / 'docs/evidence/swebench-pro-20-batch4-2026-10-12'
SOURCE = DOC.parent / 'swebench-pro-40-batch3-2026-10-11/publish.py'


def export_fixture(tmp_path, monkeypatch, *, memory_called):
    doc = tmp_path / 'docs/evidence/swebench-pro-20-batch4-2026-10-12'
    out = tmp_path / 'out'
    (out / 'live').mkdir(parents=True)
    (out / 'live/ledger.jsonl').write_text('')
    for folder, called in [('memory-attempts', memory_called), ('final-unattempted', True)]:
        destination = doc / folder / 'b4-09'
        destination.mkdir(parents=True)
        (destination / 'task-outcome.json').write_text(json.dumps({
            'instance_id': 'fixed-9', 'main_model_was_called': called,
            'actual_agent_attempt': int(called), 'official_resolved': None,
            'source_stage': folder,
        }))
    (doc / 'task-manifest.json').write_text(json.dumps({'tasks': [{'instance_id': 'fixed-9'}]}))
    original = Path.read_text
    source = original(SOURCE).replace("ROOT=Path('/home/pyh/nzcoder')", f'ROOT=Path({str(tmp_path)!r})')
    source = source.replace('LIVE=OUT/STAGE;', f'OUT=Path({str(out)!r});LIVE=OUT/STAGE;')

    def read(path, *args, **kwargs):
        return source if path == SOURCE else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(sys, 'argv', ['publish.py', 'live'])
    runpy.run_path(str(DOC / 'publish.py'), run_name='__main__')
    return json.loads((doc / 'current-outcomes.json').read_text())


def test_late_first_model_attempt_replaces_only_prior_zero_call_record(tmp_path, monkeypatch):
    results = export_fixture(tmp_path, monkeypatch, memory_called=False)
    assert results[0]['source_stage'] == 'final-unattempted'
    assert results[0]['main_model_was_called'] is True


def test_export_still_rejects_replacement_of_paid_attempt(tmp_path, monkeypatch):
    with pytest.raises(AssertionError, match='refuse_to_replace_paid_attempt'):
        export_fixture(tmp_path, monkeypatch, memory_called=True)
