"""Controlled merged-stream fixture: marker is generated only during execution."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import sys
import traceback


def main():
    line = 100_000 + secrets.randbelow(900_000)
    # Independent oracle, never part of the command or assistant response.
    Path("expected.json").write_text(json.dumps({"line": line,
        "marker": f"SyntaxError (line {line}): invalid syntax"}))
    print("Authorization: Bearer F04_PRIVATE_SENTINEL", flush=True)
    for index in range(3000):
        print(f"synthetic {index:04d} 中文 " + "x" * 80)
    sys.stdout.flush()
    try:
        compile("\n" * (line - 1) + "def broken(:", "fixture.py", "exec")
    except SyntaxError as exc:
        traceback.print_exception(exc)
    print("Cookie: F04_PRIVATE_SENTINEL\x1b[2J", file=sys.stderr)
    return 7


if __name__ == "__main__":
    raise SystemExit(main())
