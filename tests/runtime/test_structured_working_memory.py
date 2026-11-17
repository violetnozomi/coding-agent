"""四区记忆的持久化、主观标签和原子更新合同。"""
import json

import pytest

from nz_coder.tools.scratchpad import Scratchpad, _update_scratchpad, scoped_scratchpad


def memory():
    return {
        'focus': [{'summary': 'NEXT_ACTION', 'details': 'FULL_DETAILS'}],
        'findings': [{'summary': 'OBSERVATION', 'details': 'DATA',
                      'certainty': 'hypothesis', 'basis': 'SOURCE'}],
        'decisions': [{'summary': 'CHOICE', 'details': 'WHY'}],
        'lessons': [{'summary': 'LESSON', 'details': 'FAILED_APPROACH'}],
    }


def test_structured_roundtrip_snapshot_and_summary(tmp_path):
    path = tmp_path / 'notes.json'
    pad = Scratchpad(path)
    body = memory()
    assert pad.replace_memory(body, expected_revision=0) is None
    body['focus'][0]['summary'] = 'MUTATED'
    snapshot, revision = pad.snapshot()
    assert revision == 1
    snapshot['focus'].clear()
    assert Scratchpad(path).memory == pad.memory
    assert pad.memory['focus'][0]['summary'] == 'NEXT_ACTION'
    assert isinstance(pad.memory['focus'][0]['ts'], float)
    assert json.loads(path.read_text())['version'] == 2
    view = pad.build_prompt_block()
    for marker in ('NEXT_ACTION', 'OBSERVATION', 'CHOICE', 'LESSON', 'hypothesis'):
        assert marker in view
    for hidden in ('FULL_DETAILS', 'SOURCE', 'FAILED_APPROACH'):
        assert hidden not in view
        assert hidden in pad.read()


@pytest.mark.parametrize('change', [
    lambda m: m.pop('lessons'),
    lambda m: m['focus'].append({'summary': '', 'details': ''}),
    lambda m: m['focus'][0].update(ts=0),
    lambda m: m['findings'][0].update(certainty='verified'),
    lambda m: m['findings'][0].pop('basis'),
    lambda m: m['decisions'][0].update(extra='no'),
])
def test_invalid_whole_replacement_is_lossless(tmp_path, change):
    path = tmp_path / 'notes.json'
    pad = Scratchpad(path)
    assert pad.replace_memory(memory(), expected_revision=0) is None
    before = path.read_bytes()
    changed = memory()
    change(changed)
    assert pad.replace_memory(changed, expected_revision=1)
    assert path.read_bytes() == before
    assert pad.revision == 1


def test_tools_exclusive_branches_and_section_clear(tmp_path):
    pad = Scratchpad(tmp_path / 'notes.json')
    with scoped_scratchpad(pad):
        assert not str(_update_scratchpad(section='focus', entries=memory()['focus'])).startswith('Error:')
        before = pad.read()
        assert str(_update_scratchpad(category='plan', content='old note')).startswith('Error:')
        assert str(_update_scratchpad(section='focus', entries=[], category='plan',
                                     content='hypothesis', next_action={'kind': 'inspect'})).startswith('Error:')
        assert pad.read() == before
        result = _update_scratchpad(category='plan', content='hypothesis', next_action={'kind': 'inspect'})
        assert 'execution_handoff_proposal' in result.metadata
        assert pad.read() == before
        assert not str(_update_scratchpad(section='focus', entries=[], mode='replace')).startswith('Error:')
        assert pad.memory['focus'] == []


@pytest.mark.parametrize('payload', [{'version': 1, 'entries': []}, {'version': 42}, None])
def test_unknown_existing_format_is_never_treated_as_empty(tmp_path, payload):
    path = tmp_path / 'notes.json'
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='format|version'):
        Scratchpad(path).clear()
    assert path.read_bytes() == before


def test_all_summaries_survive_old_display_limit(tmp_path):
    pad = Scratchpad(tmp_path / 'notes.json')
    body = memory()
    body['focus'][0]['summary'] = 'LONG_FOCUS ' * 800
    assert pad.replace_memory(body, expected_revision=0) is None
    view = pad.build_prompt_block()
    assert body['focus'][0]['summary'] in view
    assert 'LESSON' in view
    assert len(view) > 2000
    assert 'read_scratchpad' not in pad.build_prompt_block(include_read_hint=False)


def test_deepseek_wire_schema_keeps_both_branches_and_finding_fields(tmp_path):
    from nz_coder.providers.tool_schema import adapt_tool_specs
    from nz_coder.tools import get_specs
    original = next(s for s in get_specs() if s['function']['name'] == 'update_scratchpad')
    wire = adapt_tool_specs([original], provider='deepseek', model='deepseek-chat')[0]['function']
    schema = wire['parameters']
    required = set(schema.get('required', []))
    assert required <= {'category', 'content', 'next_action'}
    assert required <= {'section', 'entries'}
    assert 'not' not in schema
    entry = schema['properties']['entries']['items']
    assert {'summary', 'details', 'certainty', 'basis'} <= set(entry['properties'])
    assert 'ts' not in entry['properties']
    assert 'next_action' in wire['description']
    assert 'findings' in wire['description']
    assert len(original['function']['description']) <= 600
    pad = Scratchpad(tmp_path / 'notes.json')
    with scoped_scratchpad(pad):
        finding = {'summary': 'MODEL_LABEL_ONLY', 'details': '', 'certainty': 'confirmed', 'basis': ''}
        assert not str(_update_scratchpad(section='findings', entries=[finding])).startswith('Error:')
        assert str(_update_scratchpad(section='focus', entries=[finding])).startswith('Error:')
        assert str(_update_scratchpad(section='findings', entries=[{'summary': 'missing', 'details': ''}])).startswith('Error:')
