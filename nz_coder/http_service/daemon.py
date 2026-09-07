"""Lifecycle manager for the long-lived loopback product runtime."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, TextIO
from urllib.parse import urlsplit

from nz_coder import __version__
from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.state.diagnostics import OperationDiagnostic

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
    health_timeout = _validated_timeout(timeout, "status", 60.0)
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
        health = NZCoderClient(
            endpoint,
            "health-only",
            timeout=health_timeout,
        ).health()
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
    ready_timeout = _validated_timeout(startup_timeout, "startup", 300.0)
    interaction = _validated_timeout(interaction_timeout, "interaction", 86_400.0)
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("daemon only accepts a loopback host")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    started = time.monotonic()
    deadline = started + ready_timeout
    paths = daemon_paths(profile, state_root)
    diagnostic = OperationDiagnostic("daemon", directory=paths.root / "diagnostics" / "parent")
    diagnostic.advance("requested")
    process: subprocess.Popen | None = None
    log_handle = None
    log_offset = 0
    nonce = ""

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            diagnostic.advance("deadline_exhausted", remaining_ms=0)
            raise TimeoutError("daemon startup deadline exhausted")
        return budget

    try:
        diagnostic.advance("prepare")
        _prepare_private_dir(paths.root)
        current = daemon_status(profile, state_root=state_root, timeout=min(0.75, remaining()))
        remaining()
        if current.get("running"):
            diagnostic.advance("ready", identity_matches=True)
            return {**current, "already_running": True}
        stale_pid = current.get("pid")
        if isinstance(stale_pid, int) and _pid_alive(stale_pid):
            marker = current.get("process_identity")
            if marker and marker == _process_identity(stale_pid):
                raise RuntimeError("daemon owns a live process with an unavailable endpoint")
        # The operation ID distinguishes concurrent starters even in one process.
        _clear_stale_lock(paths.lock)
        _acquire_lock(paths.lock, owner_id=diagnostic.id)
        for path in (paths.state, paths.token):
            path.unlink(missing_ok=True)
        nonce = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        _atomic_private_text(paths.token, token + "\n")
        _ensure_private_log(paths.log)
        roots = [str(Path(item).expanduser().resolve()) for item in (workspaces or [])]
        command = [
            sys.executable, "-m", "nz_coder", "daemon", "_serve",
            "--profile", profile, "--state-root", str(paths.root.parent),
            f"--nonce={nonce}", "--host", host, "--port", str(port),
            "--interaction-timeout", str(interaction),
            "--handoff-timeout", str(remaining()),
            "--startup-deadline", str(deadline),
            "--diagnostic-id", diagnostic.id,
        ]
        for root in roots:
            command.extend(("--workspace", root))
        # Retain this attempt's descriptor through failure capture. Even if the
        # pathname disappears before worker initialization, its bytes survive.
        log_handle = paths.log.open("a+b", buffering=0)
        log_handle.seek(0, os.SEEK_END)
        log_offset = log_handle.tell()
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": log_handle,
            "close_fds": True,
            "cwd": str(Path.cwd()),
            "shell": False,
        }
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        remaining()
        diagnostic.advance("spawn", token_present=True)
        process = subprocess.Popen(command, **kwargs)
        diagnostic.advance("spawned", pid=process.pid, spawned=True)
        if process.poll() is not None:
            diagnostic.advance("child_exited", exit_code=process.returncode, alive=False)
            raise ChildProcessError("daemon exited before handoff")
        diagnostic.advance("process_identity")
        marker = _wait_for_process_identity(process.pid, deadline=deadline, process=process)
        remaining()
        starting = {
            "schema_version": 1, "status": "starting", "profile": profile,
            "pid": process.pid, "process_identity": marker, "nonce": nonce,
            "endpoint": "", "started_at": time.time(), "version": __version__,
            "log_path": str(paths.log), "token_path": str(paths.token),
            "workspaces": roots, "state_path": str(paths.state), "state": str(paths.state),
            "diagnostic_id": diagnostic.id,
        }
        diagnostic.advance("handoff", identity_matches=True)
        _atomic_state(paths.state, starting)
        diagnostic.advance("handoff", state_present=True, identity_matches=True)
        diagnostic.advance("health_check", alive=True)
        while True:
            if process.poll() is not None:
                diagnostic.advance("child_exited", exit_code=process.returncode, alive=False)
                raise ChildProcessError("daemon exited during startup")
            status = daemon_status(profile, state_root=state_root, timeout=min(0.5, remaining()))
            remaining()
            if status.get("running"):
                if (
                    status.get("pid") != process.pid or status.get("nonce") != nonce
                    or status.get("process_identity") != marker
                    or _load_state(paths.lock).get("owner_id") != diagnostic.id
                ):
                    diagnostic.advance("identity_mismatch", identity_matches=False)
                    raise RuntimeError("daemon readiness identity mismatch")
                _replace_lock_owner(
                    paths.lock, pid=process.pid, process_identity=marker, owner_id=diagnostic.id,
                )
                diagnostic.advance("ready", alive=True, identity_matches=True, endpoint_matches=True)
                return status
            if status.get("reason") in {"endpoint_identity_mismatch", "process_identity_mismatch"}:
                diagnostic.advance("identity_mismatch", identity_matches=False)
                raise RuntimeError("daemon readiness identity mismatch")
            time.sleep(min(0.05, remaining()))
    except BaseException as error:
        if isinstance(error, ChildProcessError):
            diagnostic.advance("child_exited")
        elif isinstance(error, TimeoutError):
            diagnostic.advance("deadline_exhausted")
        # Capture the primary failure and pre-cleanup facts before touching files
        # or terminating this Popen instance. Diagnostic failure is best effort.
        if isinstance(error, Exception):
            diagnostic.failure(error)
        try:
            _startup_snapshot(diagnostic, paths, process, log_handle, log_offset, started, deadline)
        except Exception as capture_error:
            diagnostic.cleanup_error(capture_error)
        _cleanup_started_instance(paths, process, nonce, diagnostic)
        if not isinstance(error, Exception):
            raise
        raise diagnostic.public_failure() from None
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception as close_error:
                diagnostic.cleanup_error(close_error)


def stop_daemon(
    profile: str = "default",
    *,
    state_root: str | Path | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Stop only the process proven to own this daemon profile."""
    stop_timeout = _validated_timeout(timeout, "stop", 300.0)
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

    deadline = time.monotonic() + stop_timeout
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
    worker.add_argument("--handoff-timeout", type=float, default=15.0)
    worker.add_argument("--startup-deadline", type=float, default=None)
    worker.add_argument("--diagnostic-id", default="")
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
    diagnostic = OperationDiagnostic(
        "daemon", directory=paths.root / "diagnostics" / "worker",
        diagnostic_id=getattr(args, "diagnostic_id", ""),
    )
    diagnostic.advance("worker_entered", pid=os.getpid(), alive=True)
    service: SessionHTTPService | None = None
    result = 4
    try:
        handoff_timeout = _validated_timeout(args.handoff_timeout, "handoff", 300.0)
        deadline = getattr(args, "startup_deadline", None)
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("invalid startup deadline")
        diagnostic.advance("handoff")
        state = _wait_for_handoff(paths.state, args.nonce, handoff_timeout, deadline=deadline)
        if not state or state.get("nonce") != args.nonce:
            diagnostic.advance("deadline_exhausted")
            raise TimeoutError("daemon handoff deadline exhausted")
        diagnostic.advance("token_read", state_present=True)
        token = _read_private_token(paths.token)
        identity = {
            "kind": "daemon", "profile": args.profile,
            "nonce": args.nonce, "version": __version__,
        }
        diagnostic.advance("service_init", token_present=True)
        service = SessionHTTPService(
            host=args.host,
            port=args.port,
            token=token,
            interaction_timeout_seconds=args.interaction_timeout,
            workspace_roots=args.workspace,
            runtime_identity=identity,
            allow_shutdown=True,
        )
        # Binding/listening is not proof of the authenticated instance's health.
        diagnostic.advance("listening", alive=True)
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
        result = 0
    except Exception as exc:
        public = diagnostic.failure(exc)
        try:
            _append_log(paths.log, str(public) + "\n")
        except Exception as log_error:
            diagnostic.cleanup_error(log_error)
        if isinstance(exc, TimeoutError) and service is None:
            result = 3
    finally:
        diagnostic.advance("cleanup", state_present=bool(_load_state(paths.state)), cleanup_attempted=True)
        cleanup_ok = True
        if service is not None:
            try:
                service.close_after_serve()
            except Exception as error:
                diagnostic.cleanup_error(error)
                cleanup_ok = False
                result = 4
        current = _load_state(paths.state)
        if current.get("nonce") == args.nonce and current.get("pid") == os.getpid():
            try:
                cleanup_ok = _remove_owned_files(paths, args.nonce, diagnostic.id, worker=True) and cleanup_ok
            except Exception as error:
                diagnostic.cleanup_error(error)
                cleanup_ok = False
        diagnostic.advance(
            "finished",
            cleanup_complete=cleanup_ok and not any(path.exists() for path in (paths.state, paths.token, paths.lock)),
        )
    return result


def _wait_for_handoff(
    path: Path, nonce: str, timeout: float, *, deadline: float | None = None,
) -> dict[str, Any]:
    """Wait within the caller's startup budget for the parent's nonce state."""
    deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
    while True:
        state = _load_state(path)
        if state.get("nonce") == nonce:
            return state
        if time.monotonic() >= deadline:
            return {}
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


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


def _acquire_lock(path: Path, *, owner_id: str = "") -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("another daemon lifecycle operation owns this profile") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "pid": os.getpid(),
            "process_identity": _process_identity(os.getpid()),
            "owner_id": owner_id,
        }) + "\n")
    harden_private_path(path)


def _replace_lock_owner(path: Path, *, pid: int, process_identity: str, owner_id: str = "") -> None:
    """Transfer the lifecycle fence from the starter to the daemon process."""
    _atomic_private_text(
        path,
        json.dumps({
            "pid": int(pid),
            "process_identity": str(process_identity),
            "owner_id": owner_id,
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
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_state(path: Path, payload: dict[str, Any]) -> None:
    _atomic_private_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ) + "\n",
    )


def _validated_timeout(value: Any, label: str, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"daemon {label} timeout must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"daemon {label} timeout must be a positive finite number"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > maximum:
        raise ValueError(
            f"daemon {label} timeout must be a positive finite number "
            f"no greater than {maximum:g} seconds"
        )
    return timeout


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


def _startup_snapshot(
    diagnostic: OperationDiagnostic, paths: DaemonPaths, process: subprocess.Popen | None,
    capture: BinaryIO | None, offset: int, started: float, deadline: float,
) -> None:
    """Persist only this attempt's bounded structural output before owned cleanup."""
    facts = {
        "spawned": process is not None,
        "alive": process is not None and process.poll() is None,
        "exit_code": process.poll() if process is not None else None,
        "state_present": paths.state.exists(),
        "token_present": paths.token.exists(),
        "log_present": paths.log.exists(),
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "remaining_ms": max(0, int((deadline - time.monotonic()) * 1000)),
        "stderr_merged": True,
    }
    try:
        if capture is not None:
            capture.seek(offset)
            bounded = capture.read(_MAX_STATE_BYTES + 1)
            truncated = len(bounded) > _MAX_STATE_BYTES
            bounded = bounded[:_MAX_STATE_BYTES]
            facts.update({
                "stderr_present": bool(bounded), "stderr_bytes": len(bounded),
                "stderr_lines": bounded.count(b"\n"), "stderr_truncated": truncated,
                "log_bytes": len(bounded), "log_truncated": truncated,
            })
        else:
            facts["stderr_present"] = False
    except Exception:
        facts["stderr_read_failed"] = True
    diagnostic.advance(diagnostic.phase, **facts)


def _remove_owned_files(paths: DaemonPaths, nonce: str, owner_id: str, *, worker: bool = False) -> bool:
    """Never remove a replacement instance's state, token, or lifecycle fence."""
    state = _load_state(paths.state)
    lock = _load_state(paths.lock)
    if state and (not nonce or state.get("nonce") != nonce):
        return False
    if lock.get("owner_id") != owner_id:
        # A directly invoked worker can own nonce state without a parent lock.
        if not (worker and not paths.lock.exists() and state.get("nonce") == nonce):
            return not any(path.exists() for path in (paths.state, paths.token, paths.lock))
    for path in (paths.state, paths.token, paths.lock):
        path.unlink(missing_ok=True)
    return True


def _cleanup_started_instance(
    paths: DaemonPaths, process: subprocess.Popen | None, nonce: str,
    diagnostic: OperationDiagnostic,
) -> None:
    """A separate two-second reap budget applies only to this retained Popen."""
    diagnostic.advance("cleanup", cleanup_attempted=True)
    reaped = process is None
    if process is not None:
        cleanup_deadline = time.monotonic() + 2.0
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception as error:
                    diagnostic.cleanup_error(error)
            try:
                process.wait(timeout=min(1.0, max(0, cleanup_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            reaped = True
        except Exception as error:
            diagnostic.cleanup_error(error)
    removed = False
    foreign = False
    if reaped:
        try:
            removed = _remove_owned_files(paths, nonce, diagnostic.id)
            foreign = not removed
        except Exception as error:
            diagnostic.cleanup_error(error)
    alive: bool | None = False
    exit_code: int | None = None
    if process is not None:
        try:
            exit_code = process.poll()
            alive = exit_code is None
        except Exception as error:
            # A diagnostic sample is not allowed to replace the primary failure
            # or turn an unknown process state into a definite liveness claim.
            diagnostic.cleanup_error(error)
            alive = None
    diagnostic.advance(
        "finished", cleanup_complete=reaped and removed,
        foreign_state_preserved=foreign,
        alive=alive,
        exit_code=exit_code,
    )


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        native = _windows_pid_alive(pid)
        # Windows does not implement the POSIX signal-0 probe.  An unknown
        # native result is conservatively treated as alive, avoiding both a
        # false stale-state cleanup and ``os.kill(pid, 0)`` raising WinError 87.
        return True if native is None else native
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


def _windows_pid_alive(pid: int, *, kernel32=None) -> bool | None:
    """Query one Windows PID through a waitable handle; ``None`` means unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        selected_kernel = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32 is None:
            selected_kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            selected_kernel.OpenProcess.restype = wintypes.HANDLE
            selected_kernel.WaitForSingleObject.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
            ]
            selected_kernel.WaitForSingleObject.restype = wintypes.DWORD
            selected_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            selected_kernel.CloseHandle.restype = wintypes.BOOL
        handle = selected_kernel.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            if kernel32 is not None and hasattr(selected_kernel, "GetLastError"):
                error = int(selected_kernel.GetLastError())
            else:
                error = int(ctypes.get_last_error())
            if error == 87:  # ERROR_INVALID_PARAMETER: PID does not exist.
                return False
            if error == 5:  # ERROR_ACCESS_DENIED: process exists but is protected.
                return True
            return None
        try:
            result = int(selected_kernel.WaitForSingleObject(handle, 0))
            if result == 0x00000102:
                return True
            if result == 0x00000000:
                return False
            return None
        finally:
            selected_kernel.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


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
        value = _windows_process_start_time(pid)
        if value:
            return f"windows:{pid}:{value}"
    return ""


def _windows_process_start_time(pid: int, *, kernel32=None) -> str:
    """Read one Windows process creation time without spawning a shell."""
    try:
        import ctypes
        from ctypes import wintypes

        selected_kernel = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32 is None:
            selected_kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            selected_kernel.OpenProcess.restype = wintypes.HANDLE
            selected_kernel.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            selected_kernel.GetProcessTimes.restype = wintypes.BOOL
            selected_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            selected_kernel.CloseHandle.restype = wintypes.BOOL
        handle = selected_kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not selected_kernel.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return ""
            value = (int(creation.dwHighDateTime) << 32) | int(
                creation.dwLowDateTime
            )
            return str(value) if value else ""
        finally:
            selected_kernel.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return ""


def _wait_for_process_identity(
    pid: int, *, deadline: float | None = None, process: subprocess.Popen | None = None,
) -> str:
    for _ in range(40):
        if process is not None and process.poll() is not None:
            raise ChildProcessError("daemon exited before process identity")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("daemon process identity deadline exhausted")
        marker = _process_identity(pid)
        if marker:
            return marker
        time.sleep(0.01 if deadline is None else min(0.01, max(0, deadline - time.monotonic())))
    raise RuntimeError("could not establish daemon process identity")


def _terminate_pid(
    pid: int,
    *,
    timeout: float,
    os_name: str | None = None,
    runner=subprocess.run,
) -> None:
    selected_os = os.name if os_name is None else os_name
    if selected_os == "nt":
        try:
            completed = runner(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, float(timeout)),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise RuntimeError(f"failed to terminate daemon PID {pid}: {exc}") from exc
        if completed.returncode != 0 and _pid_alive(pid):
            raise RuntimeError(
                f"failed to terminate daemon PID {pid}: taskkill exit {completed.returncode}"
            )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid):
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
