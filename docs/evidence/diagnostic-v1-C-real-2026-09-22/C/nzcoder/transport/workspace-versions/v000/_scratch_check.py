import json

from configkit import loads
from configkit.config.errors import ConfigError
from configkit.cli import main
from configkit.config.migrate import migrate_file
from configkit.service import export_config, inspect_config

v1 = '{"name":"demo","host":"localhost","port":8080,"enabled":true}'
v2 = '{"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":8080}},"enabled":true}'
print(loads(v1), loads(v2))
print(loads('{"name":"demo","host":"localhost","port":8080}'))
print(loads('{"name":"demo","host":"localhost","port":8080,"zzz":1,"service":{"name":"x"}}'))


def err(t):
    try:
        loads(t)
        return "NO ERROR"
    except ConfigError as e:
        return e.to_dict()


cases = [
    '{"version":3,"name":"demo","host":"h","port":1}',
    '{"name":"  ","host":"h","port":1}',
    '{"name":"d","host":"","port":1}',
    '{"name":"d","host":"h","port":0}',
    '{"name":"d","host":"h","port":65536}',
    '{"name":"d","host":"h","port":True}',
    '{"name":"d","host":"h","port":1,"enabled":"yes"}',
    '{"name":"d","host":"h"}',
    '{"version":2,"service":{"name":"d","endpoint":{"host":"h"}}}',
    '{"version":2,"service":{"endpoint":{"host":"h","port":1}}}',
    '{"version":2,"service":{"name":"d"}}',
    '{"version":2}',
    '[]',
    'not json',
    '{"name":"d","host":"h","port":1,"version":true}',
    '{"name":"d","host":"h","port":1,"version":"2"}',
]
for c in cases:
    print(c, "->", err(c))
