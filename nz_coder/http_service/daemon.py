"""Lifecycle manager for the long-lived loopback product runtime."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, TextIO
from urllib.parse import urlsplit

from nz_coder import __version__
from nz_coder.private_paths import harden_private_path

from .client import NZCoderClient
from .server import SessionHTTPService


_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_STATE_BYTES = 64 * 1024
_DEFAULT_PORT = 4096


@dataclass(frozen=True)
class DaemonPaths:
    """Private files owned by one daemon profile."""

    root: Path
    state: Path
    token: Path
    log: Path
    lock: Path


def daemon_paths(profile: str = "default", state_root: str | Path | None = None) -> DaemonPaths:
    """Resolve a profile without allowing traversal outside the state root."""
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise ValueError("daemon profile must contain only letters, numbers, '.', '_' or '-'")
    if state_root is None:
        configured = os.environ.get("NZ_DAEMON_DIR", "").strip()
        if configured:
            base = Path(configured).expanduser()
        else:
            xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
            base = (
                Path(xdg_state).expanduser() / "nz-coder" / "daemon"
                if xdg_state
                else Path.home() / ".local" / "state" / "nz-coder" / "daemon"
            )
    else:
        base = Path(state_root).expanduser()
    root = base.resolve() / profile
    return DaemonPaths(
        root=root,
        state=root / "state.json",
        token=root / "token",
        log=root / "daemon.log",
        lock=root / "owner.lock",
    )


def daemon_status(
    profile: str = "default",
    *,
    state_root: str | Path | None = None,
    timeout: float = 0.75,
) -> dict[str, Any]:
    """Return ownership-validated status without trusting a PID alone."""
    paths = daemon_paths(profile, state_root)
    state = _load_state(paths.state)
    if not state:
        return {"running": False, "profile": profile, "reason": "not_started"}
    pid = state.get("pid")
    endpoint = state.get("endpoint")
    nonce = state.get("nonce")
    marker = state.get("process_identity")
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        return {**state, "running": False, "reason": "process_not_running"}
    current_marker = _process_identity(pid)
    if not isinstance(marker, str) or not marker or marker != current_marker:
        return {**state, "running": False, "reason": "process_identity_mismatch"}
    if not isinstance(endpoint, str) or not endpoint or not isinstance(nonce, str):
        return {**state, "running": False, "reason": "invalid_state"}
    try:
        health = NZCoderClient(endpoint, "health-only", timeout=timeout).health()
    except Exception as exc:
        return {
            **state,
            "running": False,
            "reason": "health_unavailable",
            "detail": type(exc).__name__,
        }
    runtime = health.get("runtime") if isinstance(health, dict) else None
    if (
        not isinstance(runtime, dict)
        or runtime.get("nonce") != nonce
        or runtime.get("profile") != profile
        or health.get("pid") != pid
    ):
        return {**state, "running": False, "reason": "endpoint_identity_mismatch"}
    return {**state, "running": True, "reason": "ready", "health": health}


def start_daemon(
    *,
    profile: str = "default",
    state_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    workspaces: list[str] | None = None,
    interaction_timeout: float = 300.0,
    startup_timeout: float = 15.0,
) -> dict[str, Any]:
    """Spawn a detached product runtime and wait for identity-checked health."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("daemon only accepts a loopback host")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    paths = daemon_paths(profile, state_root)
    _prepare_private_dir(paths.root)
    current = daemon_status(profile, state_root=state_root)
    if current.get("running"):
        return {**current, "already_running": True}
    stale_pid = current.get("pid")
    if isinstance(stale_pid, int) and _pid_alive(stale_pid):
        marker = current.get("process_identity")
        if marker and marker == _process_identity(stale_pid):
            raise RuntimeError(
                "daemon state owns a live process but its endpoint is unavailable; stop it first"
            )
    # State may be stale, but an extant lock belongs to another lifecycle
    # operation until proven otherwise. Acquire first; only its owner may
    # replace state/token files.
    _clear_stale_lock(paths.lock)
    _acquire_lock(paths.lock)
    for path in (paths.state, paths.token):
        path.unlink(missing_ok=True)
    nonce = secrets.token_urlsafe(24)
    token = secrets.token_urlsafe(32)
    _atomic_private_text(paths.token, token + "\n")
    _ensure_private_log(paths.log)
    roots = [str(Path(item).expanduser().resolve()) for item in (workspaces or [])]
    command = [
        sys.executable,
        "-m",
        "nz_coder",
        "daemon",
        "_serve",
        "--profile",
        profile,
        "--state-root",
        str(paths.root.parent),
        f"--nonce={nonce}",
        "--host",
        host,
        "--port",
        str(port),
        "--interaction-timeout",
        str(interaction_timeout),
    ]
    for root in roots:
        command.extend(("--workspace", root))
    log_handle = paths.log.open("ab", buffering=0)
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": log_handle,
            "close_fds": True,
            "cwd": str(Path.cwd()),
        }
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
    starting = {
        "schema_version": 1,
        "status": "starting",
        "profile": profile,
        "pid": process.pid,
        "process_identity": _wait_for_process_identity(process.pid),
        "nonce": nonce,
        "endpoint": "",
        "started_at": time.time(),
        "version": __version__,
        "log_path": str(paths.log),
        "token_path": str(paths.token),
        "workspaces": roots,
        "state_path": str(paths.state),
        "state": str(paths.state),
    }
    _atomic_state(paths.state, starting)
    deadline = time.monotonic() + max(0.1, float(startup_timeout))
    last_status: dict[str, Any] = starting
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        last_status = daemon_status(profile, state_root=state_root, timeout=0.5)
        if last_status.get("running"):
            _replace_lock_owner(
                paths.lock,
                pid=process.pid,
                process_identity=starting["process_identity"],
            )
            return last_status
        time.sleep(0.05)
    if process.poll() is None and _process_identity(process.pid) == starting["process_identity"]:
        _terminate_pid(process.pid, timeout=2.0)
    _remove_runtime_files(paths, keep_log=True)
    reason = str(last_status.get("reason") or f"exit_{process.poll()}")
    raise RuntimeError(f"daemon did not become ready: {reason}; see {paths.log}")


def stop_daemon(
    profile: str = "default",
    *,
    state_root: str | Path | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Stop only the process proven to own this daemon profile."""
    paths = daemon_paths(profile, state_root)
    state = _load_state(paths.state)
    if not state:
        _remove_runtime_files(paths, keep_log=True)
        return {"stopped": True, "was_running": False, "profile": profile}
    pid = state.get("pid")
    marker = state.get("process_identity")
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        _remove_runtime_files(paths, keep_log=True)
        return {"stopped": True, "was_running": False, "profile": profile}
    if not isinstance(marker, str) or marker != _process_identity(pid):
        raise RuntimeError("refusing to stop: daemon PID ownership could not be verified")

    endpoint = state.get("endpoint")
    nonce = str(state.get("nonce") or "")
    try:
        status = daemon_status(profile, state_root=state_root, timeout=0.75)
        if bool(status.get("running")):
            token = _read_private_token(paths.token)
            NZCoderClient(str(endpoint), token, timeout=2).shutdown(nonce=nonce)
    except Exception:
        pass

    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid):
        if marker != _process_identity(pid):
            raise RuntimeError("refusing fallback termination after process identity changed")
        _terminate_pid(pid, timeout=2.0)
    _remove_runtime_files(paths, keep_log=True)
    return {"stopped": True, "was_running": True, "profile": profile}


def daemon_main(argv: list[str] | None = None, *, output: TextIO | None = None) -> int:
    """Dispatch daemon lifecycle commands."""
    stream = output or sys.stdout
    parser = argparse.ArgumentParser(prog="nz-coder daemon")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "restart"):
        command = commands.add_parser(name)
        _add_profile(command)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=None)
        command.add_argument("--workspace", action="append", default=[])
        command.add_argument("--interaction-timeout", type=float, default=300.0)
        command.add_argument("--startup-timeout", type=float, default=15.0)
    status_parser = commands.add_parser("status")
    _add_profile(status_parser)
    stop_parser = commands.add_parser("stop")
    _add_profile(stop_parser)
    stop_parser.add_argument("--timeout", type=float, default=8.0)
    logs_parser = commands.add_parser("logs")
    _add_profile(logs_parser)
    logs_parser.add_argument("--tail", type=int, default=100)
    logs_parser.add_argument("--follow", action="store_true")
    worker = commands.add_parser("_serve", help=argparse.SUPPRESS)
    _add_profile(worker)
    worker.add_argument("--nonce", required=True)
    worker.add_argument("--host", default="127.0.0.1")
    worker.add_argument("--port", type=int, default=_DEFAULT_PORT)
    worker.add_argument("--workspace", action="append", default=[])
    worker.add_argument("--interaction-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "_serve":
            return _serve_worker(args)
        if args.command == "status":
            status = daemon_status(args.profile, state_root=args.state_root)
            if status.get("running"):
                print(
                    f"running pid={status['pid']} endpoint={status['endpoint']} "
                    f"profile={args.profile}",
                    file=stream,
                )
                return 0
            print(f"stopped profile={args.profile} reason={status['reason']}", file=stream)
            return 1
        if args.command == "stop":
            result = stop_daemon(
                args.profile,
                state_root=args.state_root,
                timeout=args.timeout,
            )
            print(
                f"stopped profile={args.profile} was_running={str(result['was_running']).lower()}",
                file=stream,
            )
            return 0
        if args.command == "logs":
            return _logs(args, stream)
        if args.command == "restart":
            previous = _load_state(daemon_paths(args.profile, args.state_root).state)
            if not args.workspace:
                args.workspace = list(previous.get("workspaces") or [])
            if args.port is None:
                endpoint = str(previous.get("endpoint") or "")
                parsed = urlsplit(endpoint) if endpoint else None
                args.port = parsed.port if parsed is not None else _DEFAULT_PORT
            stop_daemon(args.profile, state_root=args.state_root)
        if args.port is None:
            args.port = _DEFAULT_PORT
        status = start_daemon(
            profile=args.profile,
            state_root=args.state_root,
            host=args.host,
            port=args.port,
            workspaces=args.workspace,
            interaction_timeout=args.interaction_timeout,
            startup_timeout=args.startup_timeout,
        )
        suffix = " already running" if status.get("already_running") else " started"
        print(
            f"daemon{suffix} pid={status['pid']} endpoint={status['endpoint']} "
            f"profile={args.profile}",
            file=stream,
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=stream)
        return 2


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default="default")
    parser.add_argument("--state-root", default=None, help=argparse.SUPPRESS)


def _serve_worker(args: argparse.Namespace) -> int:
    paths = daemon_paths(args.profile, args.state_root)
    state = {}
    # The parent writes the hand-off state immediately after spawning.  A
    # detached child may win that race, so wait briefly for the nonce rather
    # than exiting and making a valid start flaky under load.
    for _ in range(100):
        state = _load_state(paths.state)
        if state.get("nonce") == args.nonce:
            break
        time.sleep(0.01)
    if not state or state.get("nonce") != args.nonce:
        return 3
    token = _read_private_token(paths.token)
    identity = {
        "kind": "daemon",
        "profile": args.profile,
        "nonce": args.nonce,
        "version": __version__,
    }
    service: SessionHTTPService | None = None
    try:
        service = SessionHTTPService(
            host=args.host,
            port=args.port,
            token=token,
            interaction_timeout_seconds=args.interaction_timeout,
            workspace_roots=args.workspace,
            runtime_identity=identity,
            allow_shutdown=True,
        )
        state.update({
            "status": "ready",
            "pid": os.getpid(),
            "process_identity": _process_identity(os.getpid()),
            "endpoint": service.base_url,
            "started_at": service.started_at,
            "version": __version__,
            "state_path": str(paths.state),
            "state": str(paths.state),
        })
        _atomic_state(paths.state, state)
        service.serve_forever()
        return 0
    except Exception as exc:
        _append_log(paths.log, f"daemon startup/runtime failure: {type(exc).__name__}: {exc}\n")
        return 4
    finally:
        if service is not None:
            service.close_after_serve()
        current = _load_state(paths.state)
        if current.get("nonce") == args.nonce:
            _remove_runtime_files(paths, keep_log=True)


def _logs(args: argparse.Namespace, stream: TextIO) -> int:
    path = daemon_paths(args.profile, args.state_root).log
    if not path.exists():
        print(f"No daemon log for profile={args.profile}", file=stream)
        return 1
    tail = max(0, min(int(args.tail), 100_000))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=tail) if tail else deque()
        for line in lines:
            print(line, end="", file=stream)
        if not args.follow:
            return 0
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="", file=stream, flush=True)
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return 0


def _prepare_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("daemon state directory must be a real directory")
    if os.name != "nt":
        path.chmod(0o700)
    harden_private_path(path)


def _acquire_lock(path: Path) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("another daemon lifecycle operation owns this profile") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "pid": os.getpid(),
            "process_identity": _process_identity(os.getpid()),
        }) + "\n")
    harden_private_path(path)


def _replace_lock_owner(path: Path, *, pid: int, process_identity: str) -> None:
    """Transfer the lifecycle fence from the starter to the daemon process."""
    _atomic_private_text(
        path,
        json.dumps({
            "pid": int(pid),
            "process_identity": str(process_identity),
        }, sort_keys=True) + "\n",
    )


def _clear_stale_lock(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload.get("pid") if isinstance(payload, dict) else None
        marker = payload.get("process_identity") if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        pid = None
        marker = None
    if (
        isinstance(pid, int)
        and pid > 0
        and _pid_alive(pid)
        and isinstance(marker, str)
        and marker == _process_identity(pid)
    ):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _load_state(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_STATE_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_state(path: Path, payload: dict[str, Any]) -> None:
    _atomic_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _atomic_private_text(path: Path, value: str) -> None:
    _prepare_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        if os.name != "nt":
            path.chmod(0o600)
        harden_private_path(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _ensure_private_log(path: Path) -> None:
    _prepare_private_dir(path.parent)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    if os.name != "nt":
        path.chmod(0o600)
    harden_private_path(path)


def _append_log(path: Path, value: str) -> None:
    try:
        _ensure_private_log(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(value)
    except OSError:
        pass


def _read_private_token(path: Path) -> str:
    if path.is_symlink():
        raise RuntimeError("daemon token path must not be a symlink")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise RuntimeError("daemon token is missing or invalid")
    return token


def _remove_runtime_files(paths: DaemonPaths, *, keep_log: bool) -> None:
    for path in (paths.state, paths.token, paths.lock):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if not keep_log:
        try:
            paths.log.unlink(missing_ok=True)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
        closing = value.rfind(")")
        fields = value[closing + 2:].split()
        if fields and fields[0] == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_identity(pid: int) -> str:
    """Return an OS start marker that changes when a PID is reused."""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
        closing = value.rfind(")")
        fields = value[closing + 2:].split()
        if closing > 0 and len(fields) > 19:
            return f"linux:{pid}:{fields[19]}"
    except OSError:
        pass
    if os.name != "nt":
        try:
            value = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1,
            ).strip()
            if value:
                return f"posix:{pid}:{value}"
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # PowerShell is available on supported Windows installations and gives
        # us a stable creation-time fence without adding psutil as a core
        # dependency.  An empty marker remains fail-closed for stop/status.
        try:
            value = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-Process -Id {int(pid)}).StartTime.ToFileTimeUtc()",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1,
            ).strip()
            if value:
                return f"windows:{pid}:{value}"
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def _wait_for_process_identity(pid: int) -> str:
    for _ in range(40):
        marker = _process_identity(pid)
        if marker:
            return marker
        time.sleep(0.01)
    raise RuntimeError("could not establish daemon process identity")


def _terminate_pid(pid: int, *, timeout: float) -> None:
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid) and os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


__all__ = [
    "DaemonPaths",
    "daemon_main",
    "daemon_paths",
    "daemon_status",
    "start_daemon",
    "stop_daemon",
]
