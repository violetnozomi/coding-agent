# Configkit

v1: {"name":"demo","host":"localhost","port":8080,"enabled":true}
v2: {"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":8080}},"enabled":true}

Both read into Config. All new writes use v2. enabled defaults to true.
python -m configkit migrate config.json writes v2; add --dry-run to preview
the identical object without changing bytes. python -m configkit show config.json
keeps the legacy summary. export_config leaves its source unchanged.
Errors exit 2 on CLI, with stderr:
{"error":"invalid_config","issues":[{"path":"service.endpoint.port","code":"invalid"}]}.
ConfigError remains a ValueError. Invalid input never overwrites files.
Run python -m pytest -q tests.
