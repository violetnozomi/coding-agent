"""Check real tool bindings and prove the checker rejects incompatible ports."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    checker = shutil.which("basedpyright")
    if checker is None:
        raise SystemExit("Install the CI development checker: basedpyright==1.39.9")
    positive = subprocess.run(
        [checker, "--project", str(root / "tests/typecheck/pyrightconfig.json")],
        cwd=root,
        check=False,
    )
    if positive.returncode != 0:
        return positive.returncode
    with tempfile.TemporaryDirectory(prefix="nz-tool-contract-") as temporary:
        directory = Path(temporary)
        fixture = directory / "incompatible.py"
        fixture.write_text(
            "from nz_coder.runtime.core.tool_contracts import ToolCheckpoint, ToolExecutorPort\n"
            "async def bad_checkpoint(status: str) -> None: pass\n"
            "checkpoint: ToolCheckpoint = bad_checkpoint\n"
            "class BadExecutor:\n"
            "    def execute_one(self, tool_call: dict, index: int) -> str: return 'not a result'\n"
            "executor: ToolExecutorPort = BadExecutor()\n",
            encoding="utf-8",
        )
        config = directory / "pyrightconfig.json"
        config.write_text(
            json.dumps(
                {
                    "include": [str(fixture)],
                    "extraPaths": [str(root)],
                    "pythonVersion": "3.10",
                    "typeCheckingMode": "basic",
                }
            ),
            encoding="utf-8",
        )
        negative = subprocess.run(
            [checker, "--project", str(config), "--outputjson"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if negative.returncode != 1:
            raise SystemExit("Expected incompatible tool ports to be rejected")
        diagnostics = json.loads(negative.stdout)["generalDiagnostics"]
        errors = [item for item in diagnostics if item.get("severity") == "error"]
        if len(errors) != 2 or not all(
            any(contract in item["message"] for item in errors)
            for contract in ("ToolCheckpoint", "ToolExecutorPort")
        ):
            raise SystemExit(
                "The checker did not reject both incompatible tool contracts"
            )
    print(
        "Positive production bindings passed; both incompatible port fixtures rejected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
