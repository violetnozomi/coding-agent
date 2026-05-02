"""Tool: bash - Run shell commands."""

import subprocess

from nz_coder import config
from nz_coder.command_policy import classify_bash, is_known_read_only_command
from nz_coder.tools import register


def run_bash(command: str, read_only: bool = False, timeout: int = None) -> str:
    classification = classify_bash(command)
    if classification["dangerous"]:
        return f"Error: Dangerous command blocked ({classification['reason']})"
    if read_only and (classification["mutating"] or not is_known_read_only_command(command)):
        return f"Error: Read-only shell blocked ({classification['reason']})"
    try:
        timeout_seconds = int(timeout or config.BASH_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return "Error: timeout must be an integer"
    if timeout_seconds < 1 or timeout_seconds > config.BASH_TIMEOUT_SECONDS:
        return f"Error: timeout must be between 1 and {config.BASH_TIMEOUT_SECONDS}s"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out ({timeout_seconds}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        prefix = f"Command exited with code {result.returncode}"
        output = f"{prefix}\n{output}" if output else prefix
    if not output:
        output = "(no output)"
    return output[:config.CONTEXT_TRUNCATE_CHARS]


register(
    name="bash",
    description="Run a shell command in the workspace. Use for running tests, installing packages, git operations, etc.",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds, 1-{config.BASH_TIMEOUT_SECONDS}. Default: {config.BASH_TIMEOUT_SECONDS}.",
            },
        },
        "required": ["command"],
    },
    handler=run_bash,
)
