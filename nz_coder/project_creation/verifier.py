"""Safe verification runner for generated projects."""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.foundation.shell_syntax import ShellSyntaxError, shell_tokens
from nz_coder.tool_platform.command_policy import classify_bash
from nz_coder.tools import ToolOutput, register
from nz_coder.runtime.adapters.verification_facts import command_receipt
from nz_coder.runtime.process.python_bytecode import _installed_identity
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.verification.domain.models import ExecutionReceipt
from nz_coder.tools.files import _safe_path


_ALLOWED_PREFIXES = (
    ("python", "-m", "py_compile"), ("python3", "-m", "py_compile"),
    ("python", "-m", "pytest"), ("python3", "-m", "pytest"), ("pytest",),
    ("npm", "test"), ("npm", "run", "build"), ("npm", "run", "typecheck"),
    ("pnpm", "test"), ("pnpm", "run", "build"), ("pnpm", "run", "typecheck"),
    ("yarn", "test"), ("yarn", "build"), ("yarn", "typecheck"),
    ("go", "test"), ("cargo", "check"), ("cargo", "test"),
)


@dataclass(frozen=True)
class ProjectPythonReceipt(ExecutionReceipt):
    """仅由本地项目验证器填写，缓存结算使用，公共工具元数据不能赋值。"""

    bytecode_identity: tuple[str, str] | None = None


def _python_launcher_identity(launcher: str, *, pytest_script: bool):
    """核实直接启动的 Python 或 pytest 脚本首行指向的解释器；未知启动器保留缺证。"""
    try:
        entry = Path(launcher)
        info = entry.stat()
        interpreter = entry
        if pytest_script:
            if not entry.is_file():
                return None
            with entry.open("rb") as stream:
                first = stream.readline(512)
            if not first.startswith(b"#!/") or not first.endswith(b"\n"):
                return None
            words = first[2:].split()
            if len(words) != 1:
                return None
            interpreter = Path(os.fsdecode(words[0]))
        if interpreter.samefile(sys.executable):
            identity = (sys.implementation.cache_tag, importlib.util.MAGIC_NUMBER.hex())
            binary = interpreter.stat()
            origin = (binary.st_dev, binary.st_ino, binary.st_ctime_ns)
        else:
            installed = _installed_identity(str(interpreter), current_workdir())
            if installed is None:
                return None
            origin, identity = installed[:-2], installed[-2:]
        return (info.st_dev, info.st_ino, info.st_ctime_ns, origin), identity
    except (OSError, RuntimeError, ValueError):
        return None


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
            commands.append(f"python -m py_compile {shlex.quote(rel)}")

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


def _verification_argv(command: str) -> tuple[str, ...] | None:
    try:
        tokens = shell_tokens(command)
    except ShellSyntaxError:
        return None
    # ^$ 是 Go 空测试选择的字面正则，末尾的 $ 没有展开对象。
    if any(word.operator or (word.dynamic and word.value not in {"^$", "-run=^$"}) for word in tokens):
        return None
    argv = tuple(word.value for word in tokens)
    if argv[:1] == ("uvicorn",):
        # 只接受帮助本身或应用入口加帮助。其他选项可能把 --help 当作自己的值。
        if argv[1:] == ("--help",) or (len(argv) == 3 and not argv[1].startswith("-") and argv[2] == "--help"):
            return argv
        return None
    return argv if any(argv[:len(prefix)] == prefix for prefix in _ALLOWED_PREFIXES) else None


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
    receipt_command = command
    bytecode_identity = None

    def observation(status, preview, *, outcome="not_executed", exit_code=None, complete=True):
        receipt = command_receipt("verify_project_build", receipt_command, str(project_dir),
            outcome=outcome, exit_code=exit_code, output=preview, output_complete=complete,
            environment_error=status == "missing_dependency")
        if bytecode_identity is not None:
            receipt = ProjectPythonReceipt(**vars(receipt), bytecode_identity=bytecode_identity)
        if status == "passed" and "test" in receipt.capabilities and (
            receipt.test_count == 0 or not receipt.reliable or not receipt.output_complete
        ):
            status = "unverified"
        return {"command": command, "status": status, "preview": preview[:800], "receipt": receipt}

    classification = classify_bash(command)
    if classification.get("dangerous"):
        return observation("blocked", classification["reason"])
    argv = _verification_argv(command)
    if argv is None:
        return observation("blocked", "command not in verification allowlist")
    # 回执和直接启动的参数保持一致，Shell 注释等未执行的文字不能进入检查身份。
    receipt_command = shlex.join(argv)
    launcher, origin = None, None
    if argv[0] in {"python", "python3", "pytest"}:
        search_path = os.pathsep.join(str(project_dir / part) for part in os.get_exec_path())
        launcher = shutil.which(argv[0], path=search_path)
        if launcher:
            origin = _python_launcher_identity(launcher, pytest_script=argv[0] == "pytest")
    try:
        result = subprocess.run(
            argv,
            executable=launcher,
            cwd=project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.PROJECT_VERIFY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return observation("missing_dependency", str(exc))
    except subprocess.TimeoutExpired:
        return observation("timeout", f"timed out after {config.PROJECT_VERIFY_TIMEOUT_SECONDS}s",
                           outcome="timed_out", complete=False)

    if origin is not None and _python_launcher_identity(launcher, pytest_script=argv[0] == "pytest") == origin:
        bytecode_identity = origin[1]
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == 0:
        status = "passed"
    elif _looks_like_missing_dependency(output, project_dir):
        status = "missing_dependency"
    else:
        status = "failed"
    return observation(status, output, outcome="succeeded" if result.returncode == 0 else "failed",
                       exit_code=result.returncode)


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
        elif any(item["status"] in {"failed", "timeout", "blocked"} for item in results):
            prefix = "FAIL: project build verification failed"
        elif any(item["status"] == "missing_dependency" for item in results):
            prefix = "WARN: project build verification needs local dependencies"
        else:
            prefix = "WARN: no confirmed execution of test cases for one or more checks"

        lines = [prefix]
        for item in results:
            lines.append(f"- [{item['status']}] {item['command']}")
            if item.get("preview"):
                lines.append(f"  {item['preview'].replace(chr(10), ' ')[:240]}")
        if any(item["status"] == "missing_dependency" for item in results):
            lines.append("Next step: create a virtualenv, install project dependencies, then rerun the blocked command.")
        return ToolOutput("\n".join(lines), execution_facts=tuple(item["receipt"] for item in results))
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
