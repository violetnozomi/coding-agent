# Settings store

Versioned, validated, atomically-written JSON settings shared by the CLI
(`cli.update`) and sync (`sync.merge`) consumers.  Standard library only, no
network access.

## On-disk format

### v1 (legacy)

A flat JSON object whose values are JSON scalars, e.g.

```json
{"name": "雪", "count": 3, "active": true}
```

A v1 file has **revision 0**.  Any JSON object that does not carry all three
reserved envelope keys is treated as v1 data -- including a dictionary with
only a `"version"` key, e.g. `{"version": 2}`.

### v2 (current)

Exactly: 

```json
{"version": 2, "revision": 1, "data": {"name": "雪", "count": 3}}
```

* `version` must be the integer `2`.
* `revision` must be a **positive** integer (`bool` is rejected even though it
  is an `int` subclass).
* `data` is a data dictionary (see below).
* No other top-level keys are allowed.

A top-level object is interpreted as an envelope when it contains **all three**
reserved keys (`version`, `revision`, `data`).

### Data dictionaries

`data` is a dictionary with **string keys** and JSON **scalar** values:
`str`, `int`, finite `float`, `bool` or `None`.  Nested containers, non-string
keys and non-finite floats (`NaN`, `Infinity`, `-Infinity`) are rejected with
`ValueError` when you pass them to `save`/`update`/`merge`.

## API

| Function | Result |
| --- | --- |
| `store.load(path)` | independent `dict` of data; a missing file yields `{}` |
| `store.load_snapshot(path)` | frozen `Snapshot(revision: int, data: dict)` |
| `store.save(path, data, *, expected_revision=None)` | the new `revision` (`int`) |
| `cli.update(path, key, value, expected_revision=None)` | the new data `dict` |
| `sync.merge(path, updates, expected_revision=None)` | the new `revision` (`int`) |

Returned dictionaries are independent copies, so mutating them never changes
the stored file.  `save` never mutates the dictionary you hand it.

## Errors

All library errors derive from `store.StoreError`:

* `store.CorruptStoreError` -- the file exists but is not a valid v1/v2 store:
  malformed JSON, invalid UTF-8, a non-object top level, duplicate keys at any
  nesting level, an unsupported `version`, a non-positive / non-integer
  `revision` (including `bool`), invalid envelope `data`, or extra envelope
  fields.
* `store.ConflictError` -- `expected_revision` was supplied and does not match
  the stored revision.

`ValueError` (not a `StoreError`) is raised when *you* supply invalid data or an
invalid `expected_revision`; validation always happens before the file is read
or written.

## Atomic saves and rollback

`save` validates the data and `expected_revision` first, then reads and
validates the current file (which is why a corrupt file is **never** silently
replaced), then checks `expected_revision`, and only then writes:

1. a unique temporary file is created in the destination directory,
2. UTF-8 JSON is written, flushed and `fsync`ed,
3. the temp file's permission bits are set (existing destination bits are
   preserved; new files get the process default), and
4. `os.replace` atomically moves it over the destination.

If anything fails before `os.replace`, the original bytes are untouched (or the
destination stays absent), the temporary file is removed, and the original I/O
exception is propagated.  Successful saves upgrade v1 content to v2 and
increment the revision; no sidecar/temporary files remain afterwards.

## Concurrency boundary

Concurrent writers are **not** promised serializability.  `save` reads,
validates and then replaces the file, but it does not hold a cross-process
lock, so two writers that interleave can still lose an update.  Use
`expected_revision` to detect *sequential* stale writes (e.g. a sync loop that
saw revision 4 will get `ConflictError` instead of clobbering revision 5); it is
not a mutex, and it cannot protect against a writer that passes
`expected_revision=None`.

## Verification

```
python -m pytest -q tests
```
