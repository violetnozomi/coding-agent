"""Sync-facing helper for the settings store."""

import store


def merge(path, updates, expected_revision=None):
    """Merge ``updates`` into the stored settings and return the new revision.

    Unrelated entries are preserved.  ``expected_revision`` is forwarded to
    :func:`store.save`.
    """
    data = store.load(path)
    data.update(updates)
    return store.save(path, data, expected_revision=expected_revision)
