"""Build a wheel and verify the installed CLI outside the source checkout."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import re
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nz-coder-release-") as directory:
        temporary = Path(directory)
        wheel_dir = temporary / "wheel"
        wheel_dir.mkdir()
        _run([
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(ROOT),
        ])
        _run([
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(wheel_dir),
            str(ROOT),
        ])
        wheels = list(wheel_dir.glob("nz_coder-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one wheel, found {len(wheels)}")
        wheel = wheels[0]
        sdists = list(wheel_dir.glob("nz_coder-*.tar.gz"))
        if len(sdists) != 1:
            raise RuntimeError(f"Expected one sdist, found {len(sdists)}")
        _inspect_wheel(wheel)

        environment = temporary / "venv"
        _create_environment(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        installed_environment = _installed_environment()
        _run(
            [str(python), "-m", "pip", "install", str(wheel)],
            env=installed_environment,
        )
        workspace = temporary / "workspace"
        workspace.mkdir()
        help_result = _run(
            [str(python), "-m", "nz_coder", "--help"],
            cwd=workspace,
            env=installed_environment,
        )
        if "nz-coder" not in help_result.stdout:
            raise RuntimeError("Installed CLI help did not identify nz-coder")
        startup_time_ms = _measure_cli_startup(
            python,
            workspace,
            installed_environment,
        )
        location = _run([
            str(python),
            "-c",
            "import nz_coder; print(nz_coder.__file__)",
        ], cwd=workspace, env=installed_environment).stdout.strip()
        if str(ROOT) in location:
            raise RuntimeError("Smoke import resolved to the source checkout")
        doctor = _run(
            [str(python), "-m", "nz_coder", "doctor", "--json"],
            cwd=workspace,
            env=installed_environment,
            check=False,
        )
        if doctor.returncode not in (0, 1):
            raise RuntimeError(f"doctor exited unexpectedly: {doctor.returncode}")
        json.loads(doctor.stdout)
        for command in (
            [str(python), "-m", "nz_coder", "run", "--help"],
            [str(python), "-m", "nz_coder", "config", "show", "--json"],
            [str(python), "-m", "nz_coder", "platform", "--json"],
        ):
            result = _run(command, cwd=workspace, env=installed_environment)
            if command[-1] == "--json":
                json.loads(result.stdout)
        _daemon_smoke(python, workspace, temporary, installed_environment)
        if os.name == "posix":
            _terminal_smoke(python, workspace)
        print("NZ_PRODUCT_METRICS " + json.dumps(
            {"startup_time_ms": startup_time_ms},
            separators=(",", ":"),
        ))
        print(f"release smoke passed: {wheel.name}")
    return 0


def _inspect_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required_suffixes = (
            "nz_coder/interface/cli.py",
            "nz_coder/doctor.py",
            "nz_coder/bundled_commands/review.md",
            "nz_coder/bundled_skills/code-review/SKILL.md",
        )
        for suffix in required_suffixes:
            if not any(name.endswith(suffix) for name in names):
                raise RuntimeError(f"Wheel is missing {suffix}")
        entry_name = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        if entry_name is None:
            raise RuntimeError("Wheel is missing console entry points")
        entries = archive.read(entry_name).decode("utf-8")
        if "nz-coder = nz_coder.interface.cli:main" not in entries:
            raise RuntimeError("Wheel is missing the nz-coder entry point")


def _create_environment(environment: Path) -> None:
    """Create an isolated release venv, tolerating distro Python without ensurepip."""
    if importlib.util.find_spec("ensurepip") is not None:
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
            return
        except (subprocess.CalledProcessError, SystemExit):
            pass
    executable = shutil.which("virtualenv")
    if executable is None:
        raise RuntimeError(
            f"{sys.executable} cannot create a venv and virtualenv is unavailable"
        )
    _run([executable, "-q", "-p", sys.executable, str(environment)])


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
):
    completed = subprocess.run(
        command,
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no subprocess output").strip()
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def _measure_cli_startup(
    python: Path,
    workspace: Path,
    environment: dict[str, str],
    *,
    run=None,
    clock=None,
) -> float:
    """Measure a source-external installed CLI cold-start command."""
    runner = run or _run
    timer = clock or time.perf_counter
    started = timer()
    runner(
        [str(python), "-m", "nz_coder", "--version"],
        cwd=workspace,
        env=environment,
    )
    return round((timer() - started) * 1000, 3)


def _terminal_smoke(python: Path, workspace: Path) -> None:
    """Exercise the installed composer, slash menu, and Ctrl+C in a real PTY."""
    import pty
    import fcntl
    import struct
    import termios

    master, slave = pty.openpty()
    environment = _installed_environment()
    environment.update({"TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30"})
    process = subprocess.Popen(
        [str(python), "-m", "nz_coder"],
        cwd=workspace,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    chunks: list[bytes] = []

    def read_for(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.05)
            if not readable:
                continue
            try:
                chunks.append(os.read(master, 65_536))
            except OSError:
                return

    def resize(columns: int, rows: int) -> None:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        process.send_signal(signal.SIGWINCH)

    try:
        # prompt_toolkit first probes cursor-position support; wait until that
        # bounded negotiation has settled before sending product keystrokes.
        read_for(3.0)
        os.write(master, b"/")
        read_for(0.7)
        os.write(master, b"\x03")  # clear the slash draft
        read_for(0.25)
        os.write(master, b"/help\r")
        read_for(0.8)
        resize(150, 40)
        read_for(0.3)
        os.write(master, b"/status\r")
        read_for(0.6)
        os.write(master, b"/keybind messages_next c-n\r")
        read_for(0.5)
        os.write(master, b"/keybind reset\r")
        read_for(0.5)
        resize(80, 24)
        read_for(0.3)
        os.write(master, b"\x03")  # arm exit without rebuilding the prompt
        read_for(0.25)
        os.write(master, b"\x03")  # confirm exit
        read_for(1.0)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)

    raw = b"".join(chunks)
    plain = re.sub(
        r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])",
        "",
        raw.decode("utf-8", errors="replace"),
    ).replace("\r", "")
    checks = {
        "composer": (
            (
                "NZ-Coder · IDLE" in plain
                or "NZ-Coder · RUNNING" in plain
                or "NZ-Coder Session" in plain
            )
            and "❯" in plain
        ),
        "credential-free startup": "Credential missing for provider" in plain,
        "slash completion": "Show available commands" in plain,
        "same-screen command": "Show persisted Session usage and cost" in plain,
        "resize and second command": "Workspace" in plain,
        "keybinding hot update": (
            "messages_next: c-n" in plain
            and "keybindings reset" in plain
        ),
        "single alternate screen": (
            raw.count(b"\x1b[?1049h") == 1
            and raw.count(b"\x1b[?1049l") == 1
        ),
        "double Ctrl+C": process.returncode == 0 and "Goodbye!" in plain,
        "traceback-free exit": "Traceback" not in plain,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        tail = " ".join(plain[-2_000:].split())
        raise RuntimeError(
            "Installed terminal smoke failed: " + ", ".join(failed) + f"; tail={tail!r}"
        )


def _daemon_smoke(
    python: Path,
    workspace: Path,
    temporary: Path,
    environment: dict[str, str],
) -> None:
    """Start, inspect, and stop the installed authenticated loopback daemon."""
    daemon_environment = dict(environment)
    daemon_environment["NZ_DAEMON_DIR"] = str(temporary / "daemon")
    start = [
        str(python),
        "-m",
        "nz_coder",
        "daemon",
        "start",
        "--workspace",
        str(workspace),
        "--startup-timeout",
        "15",
    ]
    try:
        _run(start, cwd=workspace, env=daemon_environment)
        status = _run(
            [str(python), "-m", "nz_coder", "daemon", "status"],
            cwd=workspace,
            env=daemon_environment,
        )
        if "running" not in status.stdout.lower():
            raise RuntimeError("Installed daemon status did not report running")
    finally:
        _run(
            [str(python), "-m", "nz_coder", "daemon", "stop"],
            cwd=workspace,
            env=daemon_environment,
            check=False,
        )


def _credential_free_environment() -> dict[str, str]:
    """Return a release environment that cannot inherit developer credentials."""
    environment = os.environ.copy()
    for name in (
        "API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "NZ_IMAGE_DESCRIBE_API_KEY",
    ):
        environment.pop(name, None)
    return environment


def _installed_environment() -> dict[str, str]:
    """Isolate installed-product probes from the developer Python runtime."""
    environment = _credential_free_environment()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


if __name__ == "__main__":
    raise SystemExit(main())
