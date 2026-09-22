# Configkit

Configkit reads a service configuration from JSON, summarizes it, migrates it to
the v2 representation, and exports it to a new path.

## Representations

v1 (missing `version` or `version: 1`):

```json
{"name": "demo", "host": "localhost", "port": 8080, "enabled": true}
```

v2:

```json
{
  "version": 2,
  "service": {"name": "demo", "endpoint": {"host": "localhost", "port": 8080}},
  "enabled": true
}
```

Both decode to `Config(name, host, port, enabled)`. `enabled` may be absent and
then defaults to `true`. Unknown extra fields are ignored. Every new write emits
only v2, so no top-level `name`, `host` or `port` keys are produced.

## Usage

Show a summary (accepts v1 or v2 input):

```console
$ python -m configkit show config.json
{"name": "demo", "address": "localhost:8080", "enabled": true}
```

Migrate a file in place to v2 and print the resulting JSON:

```console
$ python -m configkit migrate config.json
{"enabled": true, "service": {"endpoint": {"host": "localhost", "port": 8080}, "name": "demo"}, "version": 2}
```

Preview the migration without touching the file (`--dry-run` prints the v2 JSON
and leaves the source bytes unchanged):

```console
$ python -m configkit migrate config.json --dry-run
{"enabled": true, "service": {"endpoint": {"host": "localhost", "port": 8080}, "name": "demo"}, "version": 2}
```

Migrating twice has the same semantic result, and `service.export_config(source,
destination)` writes v2 to the destination while never changing the source.

## Validation and errors

Validation runs in the order version, name, host, port, enabled and reports only
the first problem. `name` and `host` must be nonempty, non-whitespace strings;
`port` must be an integer in 1..65535 (booleans are rejected); `enabled` must be
a boolean. Unsupported versions report the `version` path with code
`unsupported`; other problems use the leaf path of the input shape (`name`,
`host`, `port`, `enabled` for v1; `service.name`, `service.endpoint.host`,
`service.endpoint.port` for v2). Malformed JSON or a non-object root reports
path `"$"`. Invalid input exits with status 2, prints the error dictionary as
JSON to stderr, and never modifies the input or an existing destination:

```console
$ python -m configkit migrate config.json; echo "exit=$?"
{"error": "invalid_config", "issues": [{"path": "port", "code": "invalid"}]}
exit=2
```

Failures raise `ConfigError`, a `ValueError` subclass whose `to_dict()` is
exactly `{"error": "invalid_config", "issues": [{"path": "...", "code": "..."}]}`.
