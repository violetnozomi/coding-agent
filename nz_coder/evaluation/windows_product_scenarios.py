"""Machine-readable Windows, terminal UX, and RC acceptance manifests."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.metadata
import os
import platform
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AcceptanceScenario:
    """One acceptance obligation with an executable evidence owner."""

    scenario_id: str
    name: str
    command: tuple[str, ...]
    native_platform: str = "any"
    evidence: str = "automated"

    def to_dict(self) -> dict:
        return asdict(self)


def windows_scenarios() -> tuple[AcceptanceScenario, ...]:
    py = (sys.executable, "-m", "pytest", "-q")
    native = py + ("tests/test_windows_native_smoke.py",)
    return (
        AcceptanceScenario(
            "W1",
            "release contracts",
            py + ("tests/test_release_smoke.py",),
            "windows",
        ),
        AcceptanceScenario("W2", "first startup", native + ("-k", "first_startup"), "windows"),
        AcceptanceScenario("W3", "PowerShell tool", native + ("-k", "powershell"), "windows"),
        AcceptanceScenario("W4", "path-with-space edit", native + ("-k", "space_path"), "windows"),
        AcceptanceScenario("W5", "Chinese path", native + ("-k", "cjk_path"), "windows"),
        AcceptanceScenario(
            "W6",
            "persistent process and Job Object binding",
            native + ("-k", "persistent_process or job_object_binding"),
            "windows",
        ),
        AcceptanceScenario("W7", "ConPTY REPL", native + ("-k", "conpty_repl"), "windows"),
        AcceptanceScenario("W8", "resize", native + ("-k", "conpty_resize"), "windows"),
        AcceptanceScenario("W9", "Ctrl+C", native + ("-k", "ctrl_c"), "windows"),
        AcceptanceScenario(
            "W10",
            "daemon and private token ACL",
            py + (
                "tests/test_daemon.py::test_daemon_start_status_stop_owns_pid_and_private_token",
            ),
            "windows",
        ),
        AcceptanceScenario("W11", "attach", py + ("tests/test_http_service.py::test_remote_session_controls_two_persistent_processes_by_identity",), "windows"),
        AcceptanceScenario("W12", "clipboard", native + ("-k", "clipboard"), "windows"),
        AcceptanceScenario("W13", "Session resume", py + ("tests/test_http_service.py::test_http_restart_discovers_and_lazily_restores_session",), "windows"),
        AcceptanceScenario("W14", "LSP", native + ("-k", "lsp_resolution"), "windows"),
        AcceptanceScenario("W15", "MCP stdio", native + ("-k", "mcp_stdio"), "windows"),
    )


def tui_scenarios() -> tuple[AcceptanceScenario, ...]:
    py = (sys.executable, "-m", "pytest", "-q")
    nodes = {
        "U1": ("First launch", "tests/test_tui_product_frames.py::test_empty_state_guides_first_task_without_a_tutorial"),
        "U2": ("No provider", "tests/test_tui_product_frames.py::test_no_provider_empty_state_has_one_actionable_path"),
        "U3": ("Normal coding", "tests/test_run_renderer.py"),
        "U4": ("Edit/diff", "tests/test_timeline.py"),
        "U5": ("Permission", "tests/test_terminal_interactions.py"),
        "U6": ("Question", "tests/test_terminal_interactions.py"),
        "U7": ("Verification fail/recover", "tests/test_run_renderer.py"),
        "U8": ("Session switch", "tests/test_terminal_backend.py"),
        "U9": ("Process", "tests/test_process_service.py"),
        "U10": ("Remote", "tests/test_terminal_backend.py"),
        "U11": ("Error retry", "tests/test_run_renderer.py"),
        "U12": ("narrow terminal", "tests/test_tui_product_frames.py::test_responsive_status_hides_secondary_metadata_under_80_columns"),
        "U13": ("Windows", "tests/test_windows_platform_runtime.py"),
        "U14": ("CJK", "tests/test_tui_product_frames.py::test_attachment_chips_are_bounded_and_cjk_safe"),
    }
    return tuple(
        AcceptanceScenario(key, name, py + (node,))
        for key, (name, node) in nodes.items()
    )


def release_scenarios(platform: str) -> tuple[AcceptanceScenario, ...]:
    family = "windows" if str(platform).lower().startswith("win") else "linux"
    py = (sys.executable, "-m", "pytest", "-q")
    owners = (
        ("R1", "release contracts", py + ("tests/test_release_smoke.py",)),
        ("R2", "headless", py + ("tests/test_headless_cli.py",)),
        ("R3", "interactive", py + ("tests/test_fullscreen.py",)),
        ("R4", "shell", py + ("tests/test_windows_shell_runtime.py",)),
        ("R5", "file edit", py + (
            "tests/test_read_file_parity.py",
            "tests/test_write_files_batch.py",
            "tests/test_changes_undo.py",
        )),
        ("R6", "Session", py + ("tests/test_timeline.py",)),
        ("R7", "process", py + ("tests/test_process_service.py",)),
        ("R8", "PTY/ConPTY", py + (("tests/test_windows_native_smoke.py" if family == "windows" else "tests/test_process_service.py"),)),
        ("R9", "daemon", py + ("tests/test_daemon.py",)),
        ("R10", "remote attach", py + ("tests/test_terminal_backend.py",)),
        ("R11", "clipboard", py + (
            "tests/test_clipboard_input.py",
            "tests/test_terminal_input.py::test_ctrl_v_binding_inserts_application_clipboard_text",
            "tests/test_terminal_input.py::test_windows_clipboard_text_uses_powershell_and_decodes_unicode",
        )),
        ("R12", "LSP/MCP", py + ("tests/test_lsp.py", "tests/test_mcp.py")),
    )
    return tuple(
        AcceptanceScenario(scenario_id, name, command, family)
        for scenario_id, name, command in owners
    )


def acceptance_manifest() -> dict:
    return {
        "schema_version": 1,
        "windows": [item.to_dict() for item in windows_scenarios()],
        "tui": [item.to_dict() for item in tui_scenarios()],
        "release": {
            "windows": [item.to_dict() for item in release_scenarios("windows")],
            "linux": [item.to_dict() for item in release_scenarios("linux")],
        },
    }


def run_acceptance_suite(
    suite: str,
    scenarios: Sequence[AcceptanceScenario],
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Execute each obligation independently and return upload-ready evidence."""
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), existing) if value
    )
    result_rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        started = time.perf_counter()
        try:
            completed = executor(
                list(scenario.command),
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=max(1.0, float(timeout_seconds)),
                check=False,
            )
            returncode = int(completed.returncode)
            output = "\n".join(
                value.strip()
                for value in (completed.stdout or "", completed.stderr or "")
                if value.strip()
            )
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            output = str(exc.stderr or exc.stdout or "") or (
                f"scenario timed out after {timeout_seconds:.1f}s"
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        result = "passed" if returncode == 0 else "failed"
        result_rows.append({
            "scenario": scenario.scenario_id,
            "name": scenario.name,
            "platform": scenario.native_platform,
            "result": result,
            "duration_ms": duration_ms,
            "failure": "" if result == "passed" else _bounded_output(output),
            "command": list(scenario.command),
            "evidence": scenario.evidence,
            "output_excerpt": _bounded_output(output),
        })
    passed = sum(row["result"] == "passed" for row in result_rows)
    return {
        "schema_version": 1,
        "suite": suite,
        "success": passed == len(result_rows),
        "environment": _environment_evidence(),
        "summary": {
            "passed": passed,
            "failed": len(result_rows) - passed,
            "total": len(result_rows),
        },
        "scenarios": result_rows,
    }


def _environment_evidence() -> dict[str, str]:
    try:
        version = importlib.metadata.version("nz-coder")
    except importlib.metadata.PackageNotFoundError:
        version = "source-checkout"
    return {
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "package_version": version,
    }


def _bounded_output(value: str, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


__all__ = [
    "AcceptanceScenario",
    "acceptance_manifest",
    "release_scenarios",
    "run_acceptance_suite",
    "tui_scenarios",
    "windows_scenarios",
]
