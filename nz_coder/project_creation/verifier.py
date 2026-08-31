"""Safe verification runner for generated projects."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.tool_platform.command_policy import classify_bash
from nz_coder.tools import register
from nz_coder.tools.files import _safe_path


_ALLOWED_PREFIXES = (
    "python -m py_compile",
    "python -m pytest",
    "pytest",
    "uvicorn ",
    "npm test",
    "npm run build",
    "npm run typecheck",
    "pnpm test",
    "pnpm run build",
    "pnpm run typecheck",
    "yarn test",
    "yarn build",
    "yarn typecheck",
    "go test",
    "cargo check",
    "cargo test",
)


def _safe_project_dir(project_dir: str) -> Path:
    path = _safe_path(project_dir or ".")
    if not path.exists() or not path.is_dir():
        raise ValueError(f"project_dir is not a directory: {project_dir}")
    return path


def _infer_commands(project_dir: Path) -> list[str]:
    commands: list[str] = []
    if (project_dir / "app/main.py").exists():
        commands.append("python -m py_compile app/main.py")
    else:
        source_candidates = [
            path for path in sorted(project_dir.rglob("*.py"))
            if "tests" not in path.parts and "__pycache__" not in path.parts
        ]
        if source_candidates:
            rel = source_candidates[0].relative_to(project_dir).as_posix()
            commands.append(f"python -m py_compile {rel}")

    if (project_dir / "tests").exists():
        commands.append("pytest")
    elif (project_dir / "package.json").exists():
        if (project_dir / "pnpm-lock.yaml").exists():
            commands.append("pnpm test")
        elif (project_dir / "yarn.lock").exists():
            commands.append("yarn test")
        else:
            commands.append("npm test")
    elif (project_dir / "go.mod").exists():
        commands.append("go test ./... -run ^$")
    elif (project_dir / "Cargo.toml").exists():
        commands.append("cargo check")
    return commands


def _is_allowed_verification_command(command: str) -> bool:
    lowered = " ".join((command or "").strip().split()).lower()
    if lowered.startswith("uvicorn "):
        return "--help" in lowered
    return any(lowered.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _looks_like_missing_dependency(output: str, project_dir: Path) -> bool:
    if "command not found" in output or "not installed" in output:
        return True
    matches = re.findall(r"No module named ['\"]([^'\"]+)['\"]", output)
    matches += re.findall(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", output)
    if not matches:
        return False
    for module_name in matches:
        top = module_name.split('.')[0]
        if (project_dir / f"{top}.py").exists() or (project_dir / top).exists():
            return False
    return True


def _run_one(command: str, project_dir: Path) -> dict:
    classification = classify_bash(command)
    if classification.get("dangerous"):
        return {"command": command, "status": "blocked", "preview": classification["reason"]}
    if not _is_allowed_verification_command(command):
        return {"command": command, "status": "blocked", "preview": "command not in verification allowlist"}
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.PROJECT_VERIFY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return {"command": command, "status": "missing_dependency", "preview": str(exc)}
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "status": "timeout",
            "preview": f"timed out after {config.PROJECT_VERIFY_TIMEOUT_SECONDS}s",
        }

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    preview = output[:800]
    if result.returncode == 0:
        status = "passed"
    elif _looks_like_missing_dependency(output, project_dir):
        status = "missing_dependency"
    else:
        status = "failed"
    return {"command": command, "status": status, "preview": preview}


def verify_project_build(project_dir: str, commands: list[str] | None = None) -> str:
    try:
        root = _safe_project_dir(project_dir)
        command_list = [str(cmd) for cmd in (commands or []) if str(cmd).strip()]
        if not command_list:
            command_list = _infer_commands(root)
        if not command_list:
            return "Error: no verification commands inferred for project"

        results = [_run_one(command, root) for command in command_list]
        if all(item["status"] == "passed" for item in results):
            prefix = "OK: project build verification passed"
        elif any(item["status"] == "missing_dependency" for item in results):
            prefix = "WARN: project build verification needs local dependencies"
        else:
            prefix = "FAIL: project build verification failed"

        lines = [prefix]
        for item in results:
            lines.append(f"- [{item['status']}] {item['command']}")
            if item.get("preview"):
                lines.append(f"  {item['preview'].replace(chr(10), ' ')[:240]}")
        if prefix.startswith("WARN:"):
            lines.append("Next step: create a virtualenv, install project dependencies, then rerun the blocked command.")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="verify_project_build",
    description=(
        "Run safe low-noise verification commands for a generated project directory without installing dependencies."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_dir": {"type": "string", "description": "Project directory relative to the workspace."},
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional explicit verification commands. If omitted, common commands are inferred.",
            },
        },
        "required": ["project_dir"],
    },
    handler=verify_project_build,
    side_effect="mutates-shell",
)
