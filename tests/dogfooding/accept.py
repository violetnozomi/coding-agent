"""Independent frozen R1 oracle, invoked outside NZ-Coder task workspaces."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROWS = [
    {"id": 1, "title": "done-first", "done": True},
    {"id": 2, "title": "open-second", "done": False},
    {"id": 3, "title": "done-third", "done": True},
    {"id": 4, "title": "open-fourth", "done": False},
]


def verify(case: str, workspace: Path) -> dict:
    checks = []
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run(*args):
        return subprocess.run([sys.executable, *args], cwd=workspace, env=env,
                              text=True, capture_output=True, timeout=30)

    def check(name, result, expected=None, code=0, error=None):
        try:
            valid = result.returncode == code
            if expected is not None:
                valid = valid and json.loads(result.stdout) == expected
            if error:
                valid = valid and error in result.stderr.lower() and not result.stdout.strip()
        except (ValueError, TypeError):
            valid = False
        checks.append({"name": name, "pass": valid, "exit_code": result.returncode})

    with tempfile.TemporaryDirectory(prefix="r1-independent-") as directory:
        path = Path(directory) / "data.json"
        if case in {"T01", "T02", "T03"}:
            def todo(*args):
                return run("-m", "todo", "--store", str(path), *args)
            path.write_text(json.dumps(ROWS))
            check("normal/default", todo("list"), ROWS)
            if case == "T01":
                path.unlink()
                check("missing", todo("list"), [])
                path.write_text("")
                check("empty", todo("list"), [])
                path.write_text("{broken-json")
                check("corrupt-read", todo("list"), code=2, error="error")
                check("corrupt-add", todo("add", "must-not-overwrite"), code=2, error="error")
                checks.append({"name": "corrupt-bytes-preserved", "pass": path.read_text() == "{broken-json"})
            else:
                for status in ("all", "open", "done"):
                    expected = [r for r in ROWS if status == "all" or r["done"] == (status == "done")]
                    check(f"status-{status}", todo("list", "--status", status), expected)
                    if case == "T03":
                        for limit in (0, 1, 2, 8):
                            check(f"{status}-limit-{limit}", todo("list", "--status", status, "--limit", str(limit)), expected[:limit])
                check("invalid-status", todo("list", "--status", "wat"), code=2, error="error")
                if case == "T03":
                    check("limit-only", todo("list", "--limit", "1"), ROWS[:1])
                    for bad in ("-1", "abc", "1.5"):
                        check(f"invalid-limit-{bad}", todo("list", "--limit", bad), code=2, error="error")
                path.write_text(json.dumps([ROWS[0]]))
                check("no-match", todo("list", "--status", "open"), [])
                help_result = todo("list", "--help")
                flags = ("--status", "--limit") if case == "T03" else ("--status",)
                checks.append({"name": "help-flags", "pass": help_result.returncode == 0 and all(f in help_result.stdout for f in flags)})
        else:
            rows = [{"level": "INFO", "module": "app"}, {"level": "ERROR", "module": "db"}, {"level": "INFO", "module": "db"}]
            path.write_text("\n".join(json.dumps(r) for r in rows))
            def logs(*args):
                return run("-m", "logstats", str(path), *args)
            check("default-level", logs(), {"ERROR": 1, "INFO": 2})
            check("level", logs("--group-by", "level"), {"ERROR": 1, "INFO": 2})
            check("module", logs("--group-by", "module"), {"app": 1, "db": 2})
            check("invalid-group", logs("--group-by", "invalid"), code=2, error="error")
            path.write_text("")
            check("empty-input", logs("--group-by", "module"), {})
            for suffix in ("{broken", '{"level":"INFO"}'):
                path.write_text(json.dumps(rows[0]) + "\n" + suffix)
                check("bad-line-no-partial-output", logs("--group-by", "module"), code=2, error="line 2")
        tests = run("-m", "unittest", "discover", "-s", "tests", "-v")
        check("visible-tests", tests)
    return {"case_id": case, "result": "PASS" if all(c["pass"] for c in checks) else "FAIL", "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=("T01", "T02", "T03", "T04"))
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    result = verify(args.case, args.workspace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["result"] == "PASS" else 1)
