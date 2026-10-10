"""Generate only R1's frozen incomplete projects, never their requested solutions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import textwrap

PROMPTS = {
    "T01": "修复这个 JSON 待办 CLI 的持久化错误：不存在或为空的存储文件应视为空任务列表；损坏的非空 JSON 返回清晰错误和非零退出码，不能被 add 静默覆盖。保持正常读写兼容，补充测试。仅修改 todo/*.py、tests/*.py、README.md。独立完成并运行测试，不使用子 Agent。",
    "T02": "给待办 CLI 的 list 子命令增加 --status open|done|all，默认 all 保持兼容。参数解析、业务过滤、JSON 输出和帮助保持一致；无匹配项返回 []，非法值明确拒绝。补充测试和最小 README 示例。仅修改 todo/*.py、tests/*.py、README.md。独立完成并运行测试，不使用子 Agent。",
    "T03": "在上一轮 --status 基础上，为 list 增加 --limit N，N 是非负整数；不提供不限制，先过滤再限制，N=0 返回 []，非法值明确拒绝。保持 --status、默认行为和测试兼容，不新增不必要的应用模块。补充测试和 README。仅修改 todo/*.py、tests/*.py、README.md，不使用子 Agent。",
    "T04": "为 JSONL 日志统计 CLI 增加 --group-by level|module，默认 level 保持现有行为。输出按分组累计的 JSON 对象；空输入 {}；损坏 JSON 或缺少字段的行必须带行号报错、exit 2、stdout 为空，不能返回部分统计。补充测试和帮助、README。仅修改 logstats/*.py、tests/*.py、README.md。独立完成并运行测试，不使用子 Agent。",
}

TODO = {
    "todo/__init__.py": '"""Small synthetic todo project."""\n',
    "todo/__main__.py": "from .cli import main\nraise SystemExit(main())\n",
    "todo/storage.py": '''
        """JSON storage for task records."""
        import json
        from pathlib import Path

        def load(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))

        def save(path, tasks):
            Path(path).write_text(json.dumps(tasks), encoding="utf-8")
    ''',
    "todo/service.py": '''
        """Task operations independent of argument parsing."""
        from . import storage

        def list_tasks(path):
            return storage.load(path)

        def add_task(path, title):
            tasks = storage.load(path)
            tasks.append({"id": max((t["id"] for t in tasks), default=0) + 1,
                          "title": title, "done": False})
            storage.save(path, tasks)
            return tasks[-1]
    ''',
    "todo/cli.py": '''
        """JSON-output command line interface."""
        import argparse
        import json
        import sys
        from . import service

        def main(argv=None):
            parser = argparse.ArgumentParser(prog="todo")
            parser.add_argument("--store", default="tasks.json")
            commands = parser.add_subparsers(dest="command", required=True)
            commands.add_parser("list", help="List tasks")
            add = commands.add_parser("add", help="Add a task")
            add.add_argument("title")
            args = parser.parse_args(argv)
            try:
                result = (service.list_tasks(args.store) if args.command == "list"
                          else service.add_task(args.store, args.title))
            except (OSError, ValueError) as exc:
                print(f"Storage error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False))
            return 0
    ''',
    "tests/test_todo.py": '''
        """Visible compatibility tests, not the external acceptance oracle."""
        import json
        from pathlib import Path
        import tempfile
        import unittest
        from todo import service

        class TodoTests(unittest.TestCase):
            def test_existing_data_and_add(self):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "tasks.json"
                    path.write_text('[{"id": 4, "title": "old", "done": true}]')
                    self.assertEqual(service.list_tasks(path)[0]["title"], "old")
                    self.assertEqual(service.add_task(path, "new")["id"], 5)
                    self.assertEqual(len(json.loads(path.read_text())), 2)
    ''',
    "README.md": "# Synthetic todo CLI\n\nRun `python -m todo --store tasks.json list` or `python -m todo --store tasks.json add TITLE`.\nOutput is JSON. Errors exit 2. Run `python -m unittest discover -s tests -v`.\n",
}

LOGSTATS = {
    "logstats/__init__.py": '"""Small synthetic JSONL statistics project."""\n',
    "logstats/__main__.py": "from .cli import main\nraise SystemExit(main())\n",
    "logstats/reader.py": '''
        """Strict JSONL reader: errors invalidate the entire input."""
        import json
        from pathlib import Path

        def read_records(path):
            rows = []
            for number, line in enumerate(Path(path).read_text().splitlines(), 1):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not all(
                        isinstance(row.get(k), str) for k in ("level", "module")
                    ):
                        raise ValueError("required string fields: level, module")
                    rows.append(row)
                except ValueError as exc:
                    raise ValueError(f"Invalid input at line {number}") from exc
            return rows
    ''',
    "logstats/service.py": '''
        """Count log levels."""
        from collections import Counter
        from .reader import read_records

        def summarize(path):
            return dict(sorted(Counter(row["level"] for row in read_records(path)).items()))
    ''',
    "logstats/cli.py": '''
        """Strict log statistics CLI."""
        import argparse
        import json
        import sys
        from .service import summarize

        def main(argv=None):
            parser = argparse.ArgumentParser(prog="logstats")
            parser.add_argument("input")
            args = parser.parse_args(argv)
            try:
                result = summarize(args.input)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(result, sort_keys=True))
            return 0
    ''',
    "tests/test_logstats.py": '''
        """Visible baseline tests."""
        from pathlib import Path
        import tempfile
        import unittest
        from logstats.service import summarize

        class LogTests(unittest.TestCase):
            def test_levels(self):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "logs.jsonl"
                    path.write_text('{"level":"INFO","module":"app"}\\n')
                    self.assertEqual(summarize(path), {"INFO": 1})
    ''',
    "README.md": "# Synthetic log statistics\n\nRun `python -m logstats logs.jsonl`. JSON output counts levels.\nAny malformed or missing-field line rejects the entire input: exit 2, line-number stderr, no stdout.\nRun `python -m unittest discover -s tests -v`.\n",
}


def generate(root: Path) -> dict:
    """Create fresh repos and freeze their base identities; refuse overwrite."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for case, files in (("T01", TODO), ("T02-T03", TODO), ("T04", LOGSTATS)):
        workspace = root / case
        workspace.mkdir()
        for name, content in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        (workspace / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
        controls = workspace / ".nz-coder"
        controls.mkdir()
        (controls / "settings.json").write_text(json.dumps({"permissions": {
            "deny": ["task", "agent_manager", "spawn_agent", "webfetch"],
        }}))
        for args in (("init", "-q"), ("add", "."),
                     ("-c", "user.name=R1 Fixture", "-c", "user.email=r1@example.invalid",
                      "commit", "-qm", "Frozen incomplete R1 fixture")):
            subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
        manifest[case] = {"directory": case, "baseline": sha}
    payload = {"fixtures": manifest, "prompts": PROMPTS}
    (root / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    print(json.dumps(generate(parser.parse_args().root), ensure_ascii=False, indent=2))
