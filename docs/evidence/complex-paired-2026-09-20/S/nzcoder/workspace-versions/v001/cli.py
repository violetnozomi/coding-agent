"""CLI-facing helper for the settings store."""

import store


def update(path, key, value, expected_revision=None):
    """Set ``key`` to ``value`` and return the new data dict.

    ``expected_revision`` is forwarded to :func:`store.save` so callers can
    detect stale writes.
    """
    data = store.load(path)
    data[key] = value
    store.save(path, data, expected_revision=expected_revision)
    return data
