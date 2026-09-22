# Configuration migration contract

Implement v2 while retaining v1 reads. CONFIG_SPEC.md is the task authority and
must not be edited. All behavior below is required; no network is involved.

## Representations
v1 is {"name":"demo","host":"localhost","port":8080,"enabled":true}.
A missing version or version=1 means v1.
v2 is {"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":8080}},"enabled":true}.
Both decode to the existing Config(name, host, port, enabled) model. enabled may
be absent and then defaults to true. Unknown extra fields are ignored.
Every new serialization/write must emit only v2 (no top-level name/host/port).
Existing consumers, show, and service.export_config must accept either input
version, preserving values. Existing Config construction and v1 read tests work.

## Validation
name and host must be nonempty strings (whitespace-only is invalid); port must
be an integer 1..65535, excluding bool; enabled must be bool.
Validate in this order: version, name, host, port, enabled, and report only the
first problem. Unsupported versions produce path "version", code "unsupported".
Missing/invalid values or missing nested containers produce code "invalid" and
the corresponding leaf path: v1 name/host/port/enabled or v2 service.name,
service.endpoint.host, service.endpoint.port, enabled.
Malformed JSON or a non-object root uses path "$", code "invalid".
Raise ConfigError, still a ValueError subclass, with to_dict() exactly:
{"error":"invalid_config","issues":[{"path":"...","code":"..."}]}.
No validation error may overwrite the input or an existing destination.

## Migration and CLI
migrate_file(path, dry_run=False) returns the v2 dictionary and writes v2 to
that file. Repeating migration has identical semantic output.
dry_run=True returns the same dictionary but leaves source bytes unchanged.
CLI: python -m configkit migrate PATH [--dry-run] prints that v2 JSON.
python -m configkit show PATH keeps its current summary JSON for either version.
Invalid config: CLI exits 2, prints the ConfigError dictionary as JSON to stderr
and does not change files. Successful commands exit 0.
Service export_config(source, destination) writes v2 to destination, never
changes source, and returns the existing summary.

## Delivery
Update docs/README.md with v1/v2 JSON examples, migrate and --dry-run usage,
and the invalid_config error shape. Add meaningful tests without dropping old
compatibility coverage. Run python -m pytest -q tests after implementation.
Report actual test results and remaining limitations.
