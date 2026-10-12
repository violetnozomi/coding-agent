from ansible.module_utils.basic import remove_values, sanitize_keys

OMIT = 'VALUE_SPECIFIED_IN_NO_LOG_PARAMETER'

assert remove_values({'key-password': 'value-password'}, frozenset(['password'])) == {'key-password': 'value-********'}
assert remove_values({'password': 'x'}, frozenset(['password'])) == {'password': 'x'}
assert remove_values({'password': 'password'}, frozenset(['password'])) == {'password': OMIT}
assert remove_values({'a': {'pong': 'secret'}}, frozenset(['pong', 'secret'])) == {'a': {'pong': OMIT}}
assert remove_values([{'base': ['balls', 'raquets']}], frozenset(['base', 'balls'])) == [{'base': [OMIT, 'raquets']}]
r = remove_values({'one': 1, 'two': 'dos', 'three': ['amigos', 'musketeers', None,
                  {'ping': 'pong', 'base': ['balls', 'raquets']}]},
                  frozenset(['balls', 'base', 'pong', 'amigos']))
assert r == {'one': 1, 'two': 'dos', 'three': [OMIT, 'musketeers', None, {'ping': OMIT, 'base': [OMIT, 'raquets']}]}, r
assert remove_values([('a', 'b')], frozenset(['a'])) == [(OMIT, 'b')]
try:
    remove_values(object(), frozenset())
    raise SystemExit('expected TypeError')
except TypeError:
    pass

assert sanitize_keys({'key-password': 'value-password'}, frozenset(['password'])) == {'key-********': 'value-password'}
assert sanitize_keys({'password': 'keep-value'}, frozenset(['password'])) == {OMIT: 'keep-value'}
assert sanitize_keys({'key-password': 'v'}, frozenset(['password']), frozenset(['key-password'])) == {'key-password': 'v'}
assert sanitize_keys({'passwordx': 1}, frozenset(['password']), frozenset(['password'])) == {'********x': 1}
assert sanitize_keys(None, frozenset(['x'])) is None
assert sanitize_keys(True, frozenset(['x'])) is True
assert sanitize_keys(5, frozenset(['5'])) == 5
assert sanitize_keys('password', frozenset(['password'])) == 'password'
assert sanitize_keys([{'password': 1}], frozenset(['password'])) == [{OMIT: 1}]
assert sanitize_keys(({'password': 1},), frozenset(['password'])) == ({OMIT: 1},)
assert sanitize_keys({'password'}, frozenset(['password'])) == {OMIT}
out = sanitize_keys({'a': {'password': {'password': 'v'}}}, frozenset(['password']))
assert out == {'a': {OMIT: {OMIT: 'v'}}}, out
nested_list = sanitize_keys([[{'password': 1}]], frozenset(['password']))
assert nested_list == [[{OMIT: 1}]], nested_list
assert sanitize_keys({'key': 'password'}, frozenset(['password'])) == {'key': 'password'}
data = []
inner = data
for _ in range(10000):
    nxt = []
    inner.append(nxt)
    inner = nxt
inner.append({'password': 1})
deep = sanitize_keys(data, frozenset(['password']))
levels = 0
cur = deep
while cur:
    assert isinstance(cur, list) and len(cur) == 1
    cur = cur[0]
    levels += 1
assert cur == {OMIT: 1}
assert levels == 10001, levels

print('ALL OK')
