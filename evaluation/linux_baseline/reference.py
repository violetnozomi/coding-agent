"""Organizer-only candidate repairs for validating assertions, NEVER agent scores.

Applied only to a separate preparation copy. No reference content is imported
by the native worker or copied into model workspaces or conversation history.
"""
from __future__ import annotations

from pathlib import Path

from .catalog import source


def apply_reference(task_id: str, repo: Path) -> None:
    def replace(name: str, before: str, after: str) -> None:
        path = repo / name
        text = path.read_text(encoding="utf-8")
        if text.count(before) != 1:
            raise ValueError("Reference no longer matches frozen fixture")
        path.write_text(text.replace(before, after), encoding="utf-8")

    def project_codec() -> None:
        replace("worklog/codec.py", "hours=str(e.hours), archived=e.archived)",
                "hours=str(e.hours), project=e.project, archived=e.archived)")
        replace("worklog/codec.py", 'archived=row.get("archived", False))',
                'project=row.get("project", ""), archived=row.get("archived", False))')

    if task_id == "T01":
        replace("textkit/parser.py", 'return [item.strip() for item in text.split(",")]',
                'return [] if not text.strip() else [item.strip() for item in text.split(",")]')
    elif task_id == "T02":
        replace("worklog/reports.py", "Decimal(str(sum(float(entry.hours) for entry in entries)))",
                "sum((entry.hours for entry in entries), Decimal(0))")
    elif task_id == "T03":
        replace("textkit/stats.py", "key=lambda item: -item[1]", "key=lambda item: (-item[1], item[0])")
    elif task_id == "T04":
        project_codec()
        replace("worklog/cli.py", "dict(title=e.title, hours=str(e.hours))",
                "dict(title=e.title, hours=str(e.hours), project=e.project)")
    elif task_id == "T05":
        replace("textkit/pipeline.py", "transform(line)", "transform(line, lower=lower)")
        replace("textkit/cli.py", "normalize_lines((stdin or sys.stdin).readlines())",
                "normalize_lines((stdin or sys.stdin).readlines(), lower=args.lower)")
    elif task_id == "T06":
        replace("worklog/store.py", "if not entry.archived]", "if include_archived or not entry.archived]")
        replace("worklog/cli.py", "select(loads((stdin or sys.stdin).read()))",
                "select(loads((stdin or sys.stdin).read()), include_archived=args.include_archived)")
    elif task_id == "T07":
        (repo / "textkit/slug.py").write_text(source('''
            """Unicode text slugs."""
            import re
            def slugify(text):
                clean = re.sub(r"[^\\w\\s-]|_", "", text.lower())
                return re.sub(r"[\\s-]+", "-", clean).strip("-")
        '''), encoding="utf-8")
        replace("textkit/__init__.py", '"""Small text processing library."""',
                '"""Small text processing library."""\nfrom .slug import slugify')
    elif task_id == "T08":
        project_codec()
        path = repo / "worklog/reports.py"
        path.write_text(path.read_text() + source('''

            def project_totals(entries):
                result = {}
                for entry in entries:
                    result[entry.project] = result.get(entry.project, Decimal(0)) + entry.hours
                return result
        '''), encoding="utf-8")
        replace("worklog/cli.py", "from .store import select",
                "from .store import select\nfrom .reports import project_totals")
        replace("worklog/cli.py", "args = parser.parse_args(argv)",
                'parser.add_argument("--summary", action="store_true")\n    args = parser.parse_args(argv)')
        replace("worklog/cli.py", "print(json.dumps(render(entries)), file=stdout or sys.stdout)",
                "value = {key: str(value) for key, value in project_totals(entries).items()} if args.summary else render(entries)\n    print(json.dumps(value), file=stdout or sys.stdout)")
    elif task_id == "T09":
        (repo / "textkit/table.py").write_text(source('''
            """Ordered mapping projection."""
            def select_columns(rows, columns):
                keys = list(dict.fromkeys(columns))
                return [{key: row.get(key) for key in keys} for row in rows]
        '''), encoding="utf-8")
        replace("textkit/__init__.py", '"""Small text processing library."""',
                '"""Small text processing library."""\nfrom .table import select_columns')
    elif task_id == "T10":
        replace("worklog/reports.py", "return total_hours(values) / len(values)",
                "return total_hours(values) / len(values) if values else Decimal(0)")
    elif task_id == "T11":
        replace("textkit/io.py", 'with Path(path).open("w", encoding="utf-8") as stream:',
                'lines = list(lines)\n    if any(not isinstance(line, str) for line in lines):\n        raise TypeError("lines must contain strings")\n    with Path(path).open("w", encoding="utf-8") as stream:')
    elif task_id == "T12":
        replace("worklog/store.py", "for entry in incoming:", "incoming = list(incoming)\n    for entry in incoming:")
        replace("worklog/store.py", "        existing.append(entry)", "    existing.extend(incoming)")
    else:
        raise ValueError("Unknown reference task")
