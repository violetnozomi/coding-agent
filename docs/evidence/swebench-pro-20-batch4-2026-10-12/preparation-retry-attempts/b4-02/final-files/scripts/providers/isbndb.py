"""Helpers for consuming ISBNdb data dumps as an Open Library import source.

The ISBNdb data dumps are newline delimited: each line holds a single JSON
record.  This module provides the small building blocks needed to turn one of
those lines into a Python dictionary and to recognise binding descriptions
that describe non-book formats (which should be excluded from the import).
"""

import json

# Binding terms (kept lowercase so matching can be case-insensitive) that
# describe a non-book format and are therefore excluded during import.
NONBOOK = [
    'audio',
    'audiobook',
    'blu-ray',
    'bluray',
    'cassette',
    'cd',
    'cd-rom',
    'cdrom',
    'dvd',
    'dvd-rom',
    'dvdrom',
    'mp3',
    'mp4',
    'vhs',
    'video',
    'vinyl',
]


def get_line(line: bytes) -> dict | None:
    """Decode a single ISBNdb dump line into a dictionary.

    :param line: a single raw line from an ISBNdb data dump, encoded as bytes
        and containing one JSON record.
    :return: the parsed JSON record, or ``None`` if the line could not be
        decoded as JSON. Malformed lines are skipped rather than raising.
    """
    try:
        return json.loads(line)
    except ValueError:
        # json.JSONDecodeError and UnicodeDecodeError both subclass ValueError.
        return None


def is_nonbook(binding: str, nonbooks: list[str]) -> bool:
    """Return whether a binding description denotes a non-book format.

    The binding string is split on spaces and every resulting word is checked
    (case-insensitively) against the supplied list of non-book terms.

    :param binding: e.g. ``"DVD"`` or ``"audio cassette"``.
    :param nonbooks: e.g. :data:`NONBOOK`.
    :return: ``True`` when any word of the binding is in ``nonbooks``.
    """
    words = binding.split(' ')
    terms = {term.casefold() for term in nonbooks}
    return any(word.casefold() in terms for word in words)
