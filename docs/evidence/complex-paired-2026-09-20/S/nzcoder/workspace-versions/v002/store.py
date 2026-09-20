"""Versioned settings store.

Two on-disk formats are supported:

* **v1 (legacy)** -- a flat JSON object whose values are JSON scalars.  Its
  revision is ``0``.
* **v2 (current)** -- exactly ``{"version": 2, "revision": <positive int>,
  "data": <flat dict>}``.

See ``README.md`` for the validation rules, the atomic-save guarantees and the
concurrency boundary.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import stat
import tempfile
from pathlib import Path

__all__ = [
    "StoreError",
    "CorruptStoreError",
    "ConflictError",
    "Snapshot",
    "load",
    "load_snapshot",
    "save",
]

VERSION = 2
_RESERVED = frozenset({"version", "revision", "data"})


class StoreError(Exception):
    """Base class for every settings-store failure."""


class CorruptStoreError(StoreError):
    """The stored file exists but is not a valid v1/v2 settings file."""


class ConflictError(StoreError):
    """The stored revision does not match the caller's expected revision."""


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """A frozen view of a stored settings file.

    ``revision`` is always a plain ``int``.  ``data`` is an independent plain
    ``dict`` copy, so mutating it never affects the store (or the next read).
    """

    revision: int
    data: dict


# ---------------------------------------------------------------------------
# parsing / validation helpers
# ---------------------------------------------------------------------------


class _Rejected(Exception):
    """Internal marker raised while decoding a rejected JSON construct."""


def _reject_constant(name):
    # ``json`` accepts NaN/Infinity/-Infinity by default; a stored settings file
    # must never contain them.
    raise _Rejected(name)


def _pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Rejected(f"duplicate key {key!r}")
        result[key] = value
    return result


def _parse(text):
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
        )
    except _Rejected as exc:
        raise CorruptStoreError(f"malformed JSON: {exc}") from exc
    except ValueError as exc:  # includes json.JSONDecodeError
        raise CorruptStoreError(f"malformed JSON: {exc}") from exc


def _validate_data(data):
    """Validate a v1/v2 data dictionary, raising ``ValueError`` on failure."""
    if not isinstance(data, dict):
        raise ValueError(f"data must be a dictionary, got {type(data).__name__}")
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(
                f"data keys must be strings, got {type(key).__name__}"
            )
        if value is None or isinstance(value, (bool, str, int)):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(
                    f"data value for {key!r} must be finite, got {value!r}"
                )
            continue
        raise ValueError(
            f"data value for {key!r} must be a JSON scalar, got {type(value).__name__}"
        )
    return data


def _validate_expected_revision(expected_revision):
    if expected_revision is None:
        return
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError(
            "expected_revision must be None or a nonnegative integer"
        )


def _decode(text):
    """Return ``(revision, data)`` for the text of a store file."""
    raw = _parse(text)
    if not isinstance(raw, dict):
        raise CorruptStoreError("top-level JSON value must be an object")

    if _RESERVED.issubset(raw):
        extra = set(raw) - _RESERVED
        if extra:
            raise CorruptStoreError(
                f"unexpected envelope fields: {sorted(extra)!r}"
            )
        version = raw["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != VERSION:
            raise CorruptStoreError(f"unsupported store version: {version!r}")
        revision = raw["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise CorruptStoreError(f"invalid revision: {revision!r}")
        data = raw["data"]
        try:
            _validate_data(data)
        except ValueError as exc:
            raise CorruptStoreError(f"invalid envelope data: {exc}") from exc
        return revision, dict(data)

    # Anything else is legacy v1 data: a flat dictionary of scalars.
    try:
        _validate_data(raw)
    except ValueError as exc:
        raise CorruptStoreError(f"invalid legacy data: {exc}") from exc
    return 0, dict(raw)


def _read(path):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, {}
    except UnicodeDecodeError as exc:
        raise CorruptStoreError(f"file is not valid UTF-8: {exc}") from exc
    return _decode(text)


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def _default_file_mode():
    current = os.umask(0)
    try:
        return 0o666 & ~current
    finally:
        os.umask(current)


def _write_atomically(path, text, mode):
    """Write ``text`` to ``path`` via a fsynced temp file + ``os.replace``.

    On any failure the destination keeps its previous bytes (or stays absent)
    and the temporary file is removed before the original exception is
    re-raised.
    """
    directory = path.parent if str(path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1  # ownership transferred to the file object
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def load(path):
    """Return an independent data ``dict``; a missing file yields ``{}``."""
    _revision, data = _read(Path(path))
    return data


def load_snapshot(path):
    """Return a frozen :class:`Snapshot` (``revision`` + independent ``data``)."""
    revision, data = _read(Path(path))
    return Snapshot(revision=revision, data=dict(data))


def save(path, data, *, expected_revision=None):
    """Validate, then atomically persist ``data`` as v2, returning the revision."""
    _validate_data(data)
    _validate_expected_revision(expected_revision)

    path = Path(path)
    current_revision, _current_data = _read(path)
    if expected_revision is not None and expected_revision != current_revision:
        raise ConflictError(
            f"expected revision {expected_revision}, found {current_revision}"
        )

    new_revision = current_revision + 1
    payload = {"version": VERSION, "revision": new_revision, "data": data}
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = _default_file_mode()

    _write_atomically(path, text, mode)
    return new_revision
