import json

import pytest

from ..providers.isbndb import NONBOOK, get_line, is_nonbook


class TestGetLine:
    def test_decodes_single_json_record(self):
        line = b'{"isbn13": "9780962561856", "title": "Sutra on Upasaka Precepts"}'
        assert get_line(line) == {
            "isbn13": "9780962561856",
            "title": "Sutra on Upasaka Precepts",
        }

    def test_preserves_nested_lists_and_non_ascii(self):
        record = {
            "title": "Café Été",
            "authors": [{"name": "José"}],
            "subjects": ["a", "b"],
        }
        line = json.dumps(record).encode()
        assert get_line(line) == record

    @pytest.mark.parametrize(
        "line",
        [
            b"",
            b"not json",
            b'{"title": "unterminated',
            b"\xff\xfe invalid utf-8",
        ],
    )
    def test_malformed_lines_return_none(self, line):
        assert get_line(line) is None


class TestIsNonbook:
    @pytest.mark.parametrize(
        "binding", ["dvd", "DVD", "Dvd", "audio cassette", "Audio Cassette"]
    )
    def test_nonbook_bindings(self, binding):
        assert is_nonbook(binding, NONBOOK) is True

    @pytest.mark.parametrize("binding", ["paperback", "Hardcover", "", "PAPERBACK"])
    def test_book_bindings(self, binding):
        assert is_nonbook(binding, NONBOOK) is False

    def test_respects_supplied_list(self):
        assert is_nonbook("Paperback", ["paperback"]) is True
        assert is_nonbook("Paperback", NONBOOK) is False


def test_nonbook_list_contents():
    assert 'dvd' in NONBOOK
    assert 'cassette' in NONBOOK
    assert 'audio' in NONBOOK
    assert 'paperback' not in NONBOOK
    assert all(term == term.lower() for term in NONBOOK)
