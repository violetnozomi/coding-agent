import web

from openlibrary.plugins.openlibrary import lists
from openlibrary.plugins.upstream import utils


def test_process_seeds():
    process_seeds = lists.lists_json().process_seeds

    def f(s):
        return process_seeds([s])[0]

    assert f("/books/OL1M") == {"key": "/books/OL1M"}
    assert f({"key": "/books/OL1M"}) == {"key": "/books/OL1M"}
    assert f("/subjects/love") == "subject:love"
    assert f("subject:love") == "subject:love"


def test_unflatten_setvalue_overwrites_simple_keys():
    # Simple (separator-free) keys must overwrite previously set values.
    assert utils.unflatten({'a': 1, 'a--0': 2})['a'] == [2]
    assert dict(utils.unflatten({'x': 'first'})).get('x') == 'first'


def test_from_input_prefers_body_over_query(monkeypatch):
    web.ctx.query = '?key=/lists/OL1L&name=from-query&seeds=/books/OL1M'
    monkeypatch.setattr(
        web,
        'data',
        lambda: b'name=Body+Name&description=Body+description'
        b'&seeds--0--key=%2Fbooks%2FOL2M',
    )
    monkeypatch.setattr(
        web,
        'input',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("web.input called")),
    )

    record = lists.ListRecord.from_input()
    assert record.key is None
    assert record.name == 'Body Name'
    assert record.description == 'Body description'
    assert record.seeds == [{'key': '/books/OL2M'}]


def test_from_input_seeds_comma_separated(monkeypatch):
    web.ctx.query = ''
    monkeypatch.setattr(web, 'data', lambda: b'name=x&seeds=%2Fbooks%2FOL1M,%2Fbooks%2FOL2M,,')

    record = lists.ListRecord.from_input()
    assert record.seeds == [{'key': '/books/OL1M'}, {'key': '/books/OL2M'}]


def test_from_input_ignores_empty_seeds(monkeypatch):
    web.ctx.query = ''
    monkeypatch.setattr(
        web,
        'data',
        lambda: b'name=x&seeds--0--key=&seeds--1--key=%2Fbooks%2FOL1M',
    )

    record = lists.ListRecord.from_input()
    assert record.seeds == [{'key': '/books/OL1M'}]


def test_from_input_defaults_not_injected_for_nested_ancestors(monkeypatch):
    web.ctx.query = ''
    # Only a nested key is provided; no default must be injected for "seeds".
    monkeypatch.setattr(web, 'data', lambda: b'seeds--0--key=%2Fbooks%2FOL1M')

    record = lists.ListRecord.from_input()
    assert record.name == ''
    assert record.description == ''
    assert record.key is None
    assert record.seeds == [{'key': '/books/OL1M'}]


def test_from_input_falls_back_to_web_input(monkeypatch):
    web.ctx.query = ''
    monkeypatch.setattr(web, 'data', lambda: b'')
    monkeypatch.setattr(
        web,
        'input',
        lambda *a, **kw: web.storage(
            key='/lists/OL1L',
            name='From Input',
            description='',
            seeds=['/books/OL1M'],
        ),
    )

    record = lists.ListRecord.from_input()
    assert record.key == '/lists/OL1L'
    assert record.name == 'From Input'
    assert record.seeds == [{'key': '/books/OL1M'}]
