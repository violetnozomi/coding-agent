"""Trusted synthetic task sources; evaluator-only checks never enter task copies."""
from __future__ import annotations

import hashlib
import json
import textwrap


AGENT_REVISION = "7c308e3a75deae112e20c0de225113fda6ec9f9e"
PILOT = ["T01", "T04"]


def source(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


REPOSITORIES = {
    "textkit": {
        "textkit/__init__.py": '"""Small text processing library."""\n',
        "textkit/parser.py": source('''
            """Comma-separated configuration values (not quoted CSV)."""
            def parse_items(text):
                return [item.strip() for item in text.split(",")]
        '''),
        "textkit/stats.py": source('''
            """Word statistics."""
            from collections import Counter

            def top_words(text, limit=3):
                counts = Counter(text.lower().split())
                return sorted(counts.items(), key=lambda item: -item[1])[:limit]
        '''),
        "textkit/pipeline.py": source('''
            """Reusable line normalization pipeline."""
            def transform(text, lower=False):
                value = " ".join(text.split())
                return value.lower() if lower else value

            def normalize_lines(lines, lower=False):
                return [transform(line) for line in lines]
        '''),
        "textkit/cli.py": source('''
            """Normalize text from standard input."""
            import argparse
            import sys
            from .pipeline import normalize_lines

            def main(argv=None, stdin=None, stdout=None):
                parser = argparse.ArgumentParser()
                parser.add_argument("--lower", action="store_true")
                args = parser.parse_args(argv)
                for line in normalize_lines((stdin or sys.stdin).readlines()):
                    print(line, file=stdout or sys.stdout)

            if __name__ == "__main__":
                main()
        '''),
        "textkit/io.py": source('''
            """Write a sequence of lines to a UTF-8 file."""
            from pathlib import Path

            def save_lines(path, lines):
                with Path(path).open("w", encoding="utf-8") as stream:
                    for line in lines:
                        if not isinstance(line, str):
                            raise TypeError("lines must contain strings")
                        stream.write(line + "\\n")
        '''),
        "tests/test_public.py": source('''
            """Existing public regression tests (not target acceptance)."""
            from textkit.parser import parse_items
            from textkit.pipeline import normalize_lines
            from textkit.stats import top_words
            from textkit.io import save_lines

            def test_parse():
                assert parse_items(" a, b ") == ["a", "b"]

            def test_normalize():
                assert normalize_lines([" Hello   World "]) == ["Hello World"]

            def test_frequency():
                assert top_words("a a b", 1) == [("a", 2)]

            def test_save(tmp_path):
                path = tmp_path / "out.txt"
                save_lines(path, ["hello", "world"])
                assert path.read_text() == "hello\\nworld\\n"
        '''),
    },
    "worklog": {
        "worklog/__init__.py": '"""JSON work log library and command line interface."""\n',
        "worklog/models.py": source('''
            """Work log domain values."""
            from dataclasses import dataclass
            from decimal import Decimal

            @dataclass
            class Entry:
                title: str
                hours: Decimal
                project: str = ""
                archived: bool = False
        '''),
        "worklog/codec.py": source('''
            """JSON persistence for entries; Decimal encoded as text."""
            import json
            from decimal import Decimal
            from .models import Entry

            def dumps(entries):
                return json.dumps([dict(title=e.title, hours=str(e.hours), archived=e.archived)
                                   for e in entries])

            def loads(text):
                return [Entry(row["title"], Decimal(row["hours"]),
                              archived=row.get("archived", False)) for row in json.loads(text)]
        '''),
        "worklog/store.py": source('''
            """In-memory entry selection and batch additions."""
            def select(entries, include_archived=False):
                return [entry for entry in entries if not entry.archived]

            def append_entries(existing, incoming):
                for entry in incoming:
                    if entry.hours < 0:
                        raise ValueError("hours must be non-negative")
                    existing.append(entry)
        '''),
        "worklog/reports.py": source('''
            """Numeric reports on work entries."""
            from decimal import Decimal

            def total_hours(entries):
                return Decimal(str(sum(float(entry.hours) for entry in entries)))

            def average_hours(entries):
                values = list(entries)
                return total_hours(values) / len(values)
        '''),
        "worklog/cli.py": source('''
            """Display a JSON work log from stdin."""
            import argparse
            import json
            import sys
            from .codec import loads
            from .store import select

            def render(entries):
                return [dict(title=e.title, hours=str(e.hours)) for e in entries]

            def main(argv=None, stdin=None, stdout=None):
                parser = argparse.ArgumentParser()
                parser.add_argument("--include-archived", action="store_true")
                args = parser.parse_args(argv)
                entries = select(loads((stdin or sys.stdin).read()))
                print(json.dumps(render(entries)), file=stdout or sys.stdout)

            if __name__ == "__main__":
                main()
        '''),
        "tests/test_public.py": source('''
            """Existing public regression tests (not target acceptance)."""
            import io
            import json
            from decimal import Decimal
            from worklog.models import Entry
            from worklog.codec import dumps, loads
            from worklog.store import select, append_entries
            from worklog.reports import total_hours, average_hours
            from worklog.cli import main

            def test_roundtrip():
                entries = [Entry("work", Decimal("2"))]
                assert loads(dumps(entries)) == entries

            def test_totals():
                entries = [Entry("a", Decimal("2")), Entry("b", Decimal("4"))]
                assert total_hours(entries) == Decimal("6")
                assert average_hours(entries) == Decimal("3")

            def test_selection_and_append():
                entries = [Entry("a", Decimal("1")), Entry("b", Decimal("1"), archived=True)]
                assert len(select(entries)) == 1
                append_entries(entries, [Entry("c", Decimal("1"))])
                assert len(entries) == 3

            def test_cli_default():
                out = io.StringIO()
                main([], io.StringIO('[{"title":"a","hours":"2"}]'), out)
                assert json.loads(out.getvalue())[0]["title"] == "a"
        '''),
    },
}

# Each assertion is independent evaluator code, not a public test copied to the
# task. Feature absence is an assertion failure, not an import/collection error.
TASK_SPECS = [
    ("T01", "single_file", "textkit", "textkit/parser.py",
     "Fix parse_items: empty or whitespace-only input returns []; preserve trimming, order, and empty fields inside a nonempty comma-separated input. Add public regression tests.",
     '''
        from textkit.parser import parse_items
        def test_empty():
            assert parse_items("") == []
        def test_whitespace():
            assert parse_items(" \\t\\n ") == []
        def test_interior_and_order():
            assert parse_items("b,, a") == ["b", "", "a"]
     '''),
    ("T02", "single_file", "worklog", "worklog/reports.py",
     "Make total_hours sum Decimal hours exactly, without float conversion. Support iterators and return Decimal(0) for an empty input. Preserve average_hours for nonempty input; add tests.",
     '''
        from decimal import Decimal
        from worklog.models import Entry
        from worklog.reports import total_hours
        def test_exact():
            assert total_hours([Entry("a", Decimal("0.1")), Entry("b", Decimal("0.2"))]) == Decimal("0.3")
        def test_large():
            assert total_hours(iter([Entry("a", Decimal("9007199254740993"))])) == Decimal("9007199254740993")
        def test_empty():
            assert total_hours(iter([])) == Decimal(0)
            assert isinstance(total_hours([]), Decimal)
     '''),
    ("T03", "single_file", "textkit", "textkit/stats.py",
     "Make top_words deterministic: frequency descending, alphabetical ascending on ties. Keep case-insensitive whitespace tokenization and the limit argument; add tests.",
     '''
        from textkit.stats import top_words
        def test_ties():
            assert top_words("z a z a b") == [("a", 2), ("z", 2), ("b", 1)]
        def test_cutoff():
            assert top_words("Z Y X", 2) == [("x", 1), ("y", 1)]
        def test_empty_zero():
            assert top_words("") == []
            assert top_words("a b", 0) == []
     '''),
    ("T04", "cross_file", "worklog", "worklog/",
     "Preserve Entry.project through JSON dumps/loads and CLI render output, including Unicode project names. Old rows without project load as ''. CLI default output must now include project; keep title/hours/archive behavior. Add tests.",
     '''
        import io, json
        from decimal import Decimal
        from worklog.models import Entry
        from worklog.codec import dumps, loads
        from worklog.cli import main
        def test_codec():
            entry = Entry("work", Decimal("2"), "研究", True)
            assert loads(dumps([entry])) == [entry]
        def test_cli():
            out = io.StringIO()
            main([], io.StringIO('[{"title":"x","hours":"1","project":"p"}]'), out)
            assert json.loads(out.getvalue())[0].get("project") == "p"
        def test_legacy():
            entry = loads('[{"title":"old","hours":"3"}]')[0]
            assert entry.project == ""
            assert json.loads(dumps([entry]))[0].get("project") == ""
     '''),
    ("T05", "cross_file", "textkit", "textkit/",
     "Wire lower=True through normalize_lines and --lower through textkit.cli, so both library and CLI lowercase normalized text when requested, while default retains case. Test both paths.",
     '''
        import io
        from textkit.pipeline import normalize_lines
        from textkit.cli import main
        def test_library():
            assert normalize_lines(iter([" ÄBC  DEF "]), lower=True) == ["äbc def"]
        def test_cli():
            out = io.StringIO()
            main(["--lower"], io.StringIO(" Ab C \\n"), out)
            assert out.getvalue() == "ab c\\n"
        def test_default():
            out = io.StringIO()
            main([], io.StringIO(" Ab C \\n"), out)
            assert out.getvalue() == "Ab C\\n"
     '''),
    ("T06", "cross_file", "worklog", "worklog/",
     "Implement include_archived=True in store.select and pass --include-archived from CLI. Default still excludes archived entries; preserve order and do not mutate the input. Add tests for both layers.",
     '''
        import io, json
        from decimal import Decimal
        from worklog.models import Entry
        from worklog.store import select
        from worklog.cli import main
        def test_select():
            values = [Entry("b", Decimal(1), archived=True), Entry("a", Decimal(2))]
            assert select(iter(values), include_archived=True) == values
            assert len(values) == 2
        def test_cli():
            out = io.StringIO()
            main(["--include-archived"], io.StringIO('[{"title":"old","hours":"1","archived":true}]'), out)
            assert [x["title"] for x in json.loads(out.getvalue())] == ["old"]
        def test_default():
            assert select([Entry("old", Decimal(1), archived=True)]) == []
     '''),
    ("T07", "small_feature", "textkit", "textkit/",
     "Add textkit.slug.slugify(text) and export it from textkit. Lowercase Unicode letters, remove punctuation except hyphens, collapse whitespace/hyphen runs into one hyphen, strip edge hyphens. Empty/punctuation-only => ''. Add tests.",
     '''
        import textkit
        def get_slugify():
            function = getattr(textkit, "slugify", None)
            assert callable(function), "public slugify export missing"
            return function
        def test_basic():
            assert get_slugify()(" Hello, World! ") == "hello-world"
        def test_separators():
            assert get_slugify()(" -- A --- B -- ") == "a-b"
        def test_unicode_empty():
            function = get_slugify()
            assert function("研究 Café") == "研究-café"
            assert function("!!!") == ""
            assert function("A_B") == "ab"
        def test_module_export():
            import importlib
            function = get_slugify()
            assert importlib.import_module("textkit.slug").slugify is function
     '''),
    ("T08", "small_feature", "worklog", "worklog/",
     "Add reports.project_totals(entries): dictionary of project to exact Decimal totals. Add CLI --summary output as a JSON object of project to decimal string; parse project from input (legacy missing => ''). Preserve ordinary list output and default archived filtering. Add tests.",
     '''
        import io, json
        from decimal import Decimal
        from worklog.models import Entry
        from worklog import reports
        from worklog.cli import main
        def test_library():
            function = getattr(reports, "project_totals", None)
            assert callable(function), "project_totals missing"
            assert function(iter([Entry("a", Decimal("0.1"), "p"), Entry("b", Decimal("0.2"), "p")])) == {"p": Decimal("0.3")}
        def test_cli():
            out = io.StringIO()
            try:
                main(["--summary"], io.StringIO('[{"title":"a","hours":"2","project":"p"}]'), out)
            except SystemExit:
                assert False, "summary flag missing"
            assert json.loads(out.getvalue()) == {"p": "2"}
        def test_empty():
            function = getattr(reports, "project_totals", None)
            assert callable(function), "project_totals missing"
            assert function([]) == {}
        def test_legacy_and_archived():
            out = io.StringIO()
            data = '[{"title":"old","hours":"1"},{"title":"archived","hours":"9","project":"p","archived":true}]'
            try:
                main(["--summary"], io.StringIO(data), out)
            except SystemExit:
                assert False, "summary flag missing"
            assert json.loads(out.getvalue()) == {"": "1"}
     '''),
    ("T09", "small_feature", "textkit", "textkit/",
     "Add textkit.table.select_columns(rows, columns), exported from textkit: produce fresh dicts in requested column order for iterable mapping rows. Missing keys map to None, duplicate columns occur once at their first position. Do not mutate inputs; support a columns iterator. Add tests.",
     '''
        import textkit
        def function():
            value = getattr(textkit, "select_columns", None)
            assert callable(value), "select_columns export missing"
            return value
        def test_order():
            rows = [{"a": 1, "b": 2}, {"a": 3}]
            result = function()(iter(rows), iter(["b", "a", "b"]))
            assert result == [{"b": 2, "a": 1}, {"b": None, "a": 3}]
            assert list(result[0]) == ["b", "a"]
            assert rows == [{"a": 1, "b": 2}, {"a": 3}]
        def test_empty_columns():
            assert function()([{"a": 1}], []) == [{}]
        def test_empty_rows():
            assert function()([], ["a"]) == []
        def test_module_export():
            import importlib
            value = function()
            assert importlib.import_module("textkit.table").select_columns is value
     '''),
    ("T10", "boundary_regression", "worklog", "worklog/reports.py",
     "Make average_hours return Decimal(0) for an empty sequence or exhausted iterator; keep nonempty average and consume iterators only once. Add regression tests.",
     '''
        from decimal import Decimal
        from worklog.models import Entry
        from worklog.reports import average_hours
        def test_empty():
            assert average_hours([]) == Decimal(0)
        def test_iterator():
            iterator = iter([Entry("a", Decimal(2))])
            assert average_hours(iterator) == Decimal(2)
            assert average_hours(iterator) == Decimal(0)
        def test_nonempty():
            assert average_hours(iter([Entry("a", Decimal(2)), Entry("b", Decimal(4))])) == Decimal(3)
     '''),
    ("T11", "boundary_regression", "textkit", "textkit/io.py",
     "Make save_lines validate/consume its input before opening the output: a non-string or a generator error must leave an existing file byte-for-byte unchanged, and must not create a new file. Preserve newline formatting. This task does not require OS/crash atomicity. Add tests.",
     '''
        import pytest
        from textkit.io import save_lines
        def test_type_error(tmp_path):
            path = tmp_path / "old"
            path.write_bytes(b"original")
            with pytest.raises(TypeError):
                save_lines(path, ["ok", 3])
            assert path.read_bytes() == b"original"
        def test_generator_error(tmp_path):
            def broken():
                yield "first"
                raise RuntimeError("input failed")
            path = tmp_path / "old"
            path.write_bytes(b"original")
            with pytest.raises(RuntimeError):
                save_lines(path, broken())
            assert path.read_bytes() == b"original"
        def test_no_create(tmp_path):
            path = tmp_path / "new"
            with pytest.raises(TypeError):
                save_lines(path, [None])
            assert not path.exists()
     '''),
    ("T12", "boundary_regression", "worklog", "worklog/store.py",
     "Make append_entries all-or-nothing for input validation: a negative hours entry or an iterator exception must leave existing unchanged. A successful batch appends in order, preserves existing identity, and handles append_entries(existing, existing) without looping. Add tests.",
     '''
        import pytest
        from decimal import Decimal
        from worklog.models import Entry
        from worklog.store import append_entries
        def test_invalid():
            old = Entry("old", Decimal(1))
            existing = [old]
            with pytest.raises(ValueError):
                append_entries(existing, [Entry("ok", Decimal(1)), Entry("bad", Decimal(-1))])
            assert existing == [old]
            assert existing[0] is old
        def test_iteration_error():
            def incoming():
                yield Entry("ok", Decimal(1))
                raise RuntimeError("input failed")
            existing = []
            with pytest.raises(RuntimeError):
                append_entries(existing, incoming())
            assert existing == []
        def test_success():
            existing = []
            items = [Entry("a", Decimal(1)), Entry("b", Decimal(2))]
            append_entries(existing, iter(items))
            assert existing == items
        def test_self_append_without_unbounded_allocation():
            class GuardedList(list):
                def __iter__(self):
                    count = len(self)
                    for index in range(count):
                        yield self[index]
                        assert len(self) == count, "input mutated during iteration"
            item = Entry("a", Decimal(1))
            existing = GuardedList([item])
            append_entries(existing, existing)
            assert existing == [item, item]
     '''),
]


def task_files(spec: tuple) -> dict[str, str]:
    files = dict(REPOSITORIES[spec[2]])
    files[".gitignore"] = "__pycache__/\n.pytest_cache/\n.nz-coder/\n"
    files["README.md"] = (
        f"# {spec[2]} — trusted local synthetic fixture\n\n"
        "Python 3.10+; standard-library runtime; pytest for tests.\n"
        "Run `python -m pytest -q tests` from this repository.\n"
    )
    return files


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def manifest() -> dict:
    return {
        "schema": 1, "agent_revision": AGENT_REVISION,
        "pilot_task_ids": PILOT, "source": "synthetic/local fixtures; not SWE-bench",
        "entry": "nz-coder run (headless CLI -> Native SDK -> Runner)",
        "tasks": [dict(
            task_id=s[0], category=s[1], repository=s[2], source="synthetic/local",
            task_revision=digest(task_files(s)), prompt=s[4],
            setup="Python >=3.10, pytest from frozen evaluator environment; no runtime dependencies",
            public_tests=["python", "-m", "pytest", "-q", "tests"],
            independent_acceptance=f"catalog.TASK_SPECS[{s[0]}] (outside task workspace)",
            acceptance_revision=digest(source(s[5])),
            allowed_paths=[s[3], "tests/"],
            forbidden_paths=[".git/", ".gitignore", "README.md", "evaluator/", "outside workspace"],
            proposed_budget={"max_turns": 30, "wall_seconds": 600,
                             "total_tokens": 100000, "cost": None, "currency": None},
        ) for s in TASK_SPECS],
    }
