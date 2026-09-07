"""Bounded operation diagnostics over the existing private TraceRecorder.

Only structural evidence is recorded: never exception messages, source lines,
locals, arguments, filesystem paths or arbitrary log text. There is no global
recorder; each operation owns its identity and best-effort persistence state.
"""
from __future__ import annotations

import asyncio
import ast
import builtins
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Lock
import uuid

from nz_coder import __version__
from nz_coder.foundation.user_paths import _secure_directory, prepare_user_storage
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.state.trace import TraceRecorder


SCHEMA = "nz.diagnostic.v1"
_PACKAGE = Path(__file__).resolve().parents[1]
_ID = re.compile(r"^diag-[0-9a-f]{32}$")
_PHASES = frozenset({
    "requested", "prepare", "spawn", "spawned", "process_identity", "handoff",
    "worker_entered", "token_read", "service_init", "listening", "health_check",
    "ready", "child_exited", "deadline_exhausted", "cleanup", "finished",
    "path_validation", "settings", "index_read", "ranking", "semantic_probe",
    "render", "failed", "unknown", "identity_mismatch",
})
_FACTS = frozenset({
    "pid", "alive", "exit_code", "state_present", "identity_matches",
    "token_present", "log_present", "log_bytes", "log_truncated", "log_read_failed",
    "stderr_present", "spawned", "cleanup_attempted", "cleanup_complete",
    "foreign_state_preserved", "files", "symbols", "generation", "cache_hits",
    "records_omitted", "elapsed_ms", "remaining_ms", "endpoint_matches",
    "stderr_bytes", "stderr_truncated", "stderr_read_failed", "stderr_lines",
    "stderr_merged",
    "index_warming", "index_failed", "index_ready",
    "record_limit_reached", "capture_failed",
    "primary_record",
})


_EXCEPTION_TYPES = tuple(
    value for value in vars(builtins).values()
    if isinstance(value, type) and issubclass(value, BaseException)
) + (
    sqlite3.Error, sqlite3.Warning, sqlite3.DatabaseError, sqlite3.OperationalError,
    sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.InternalError,
    sqlite3.ProgrammingError, sqlite3.NotSupportedError, sqlite3.DataError,
    json.JSONDecodeError, subprocess.SubprocessError, subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
)


@lru_cache(maxsize=128)
def _source_names(module: str) -> tuple[frozenset[str], frozenset[str]]:
    """Static package symbols, not a recorder or a registry of runtime state."""
    parts = module.split(".")
    if parts[0] != "nz_coder" or not all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", part) for part in parts):
        return frozenset(), frozenset()
    try:
        path = _PACKAGE.joinpath(*parts[1:]).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = frozenset(node.name for node in ast.walk(tree)
                              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        classes = frozenset(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
        return functions | {"<module>", "<lambda>"}, classes
    except Exception:
        return frozenset(), frozenset()


def safe_frame(module: str, function: str, line: int) -> dict:
    """Only source-declared location labels may enter exportable evidence."""
    functions, _ = _source_names(module)
    return {"module": module if functions else "external",
            "function": function if function in functions else "external",
            "line": line if type(line) is int and 0 < line < 1000000 else 0}


def safe_exception_label(name: str, module: str = "") -> str:
    if any(kind.__name__ == name for kind in _EXCEPTION_TYPES):
        return name
    return name if name in _source_names(module)[1] else "Exception"


def _exception_identity(error: BaseException) -> tuple[str, str]:
    try:
        kind = type(error)
        name = type.__getattribute__(kind, "__name__")
        module = type.__getattribute__(kind, "__module__")
        if kind in _EXCEPTION_TYPES or name in _source_names(module)[1]:
            return name, module
    except Exception:
        pass
    return "Exception", "builtins"


def exception_evidence(error: BaseException) -> list[dict]:
    """Capture a bounded cause chain without formatting any exception or source."""
    chain: list[dict] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(chain) < 6:
        seen.add(id(error))
        safe_type, type_module = _exception_identity(error)
        frames: deque[dict] = deque(maxlen=24)
        count = 0
        try:
            # Bypass user-defined exception attribute hooks entirely.
            tb = BaseException.__traceback__.__get__(error)
            while tb is not None and count < 1024:
                code = tb.tb_frame.f_code
                module = "external"
                try:
                    relative = Path(code.co_filename).resolve().relative_to(_PACKAGE)
                    if relative.suffix == ".py":
                        module = "nz_coder." + ".".join(relative.with_suffix("").parts)
                except (OSError, ValueError):
                    pass
                frames.append(safe_frame(module, code.co_name, tb.tb_lineno))
                count += 1
                tb = tb.tb_next
            chain.append({"type": safe_type, "type_module": type_module, "frames": list(frames),
                          "frames_omitted": max(0, count - 24), "frames_truncated": tb is not None})
            cause = BaseException.__cause__.__get__(error)
            context = BaseException.__context__.__get__(error)
            suppressed = BaseException.__suppress_context__.__get__(error)
            error = cause or (None if suppressed else context)
        except Exception:
            chain.append({"type": safe_type, "type_module": type_module,
                          "frames": list(frames), "capture_failed": True})
            break
    if error is not None and chain:
        chain[-1]["chain_truncated"] = True
    return chain


def correlation(value: str) -> str:
    """Link arbitrary existing identities by digest, without publishing their text."""
    try:
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest() if value else ""
    except Exception:
        return ""


@dataclass(frozen=True)
class CapturedFailure:
    """Private pending evidence; capture never touches disk or renders the error."""

    error: BaseException = field(repr=False)
    phase: str
    completed: str
    primary: bool


class OperationDiagnostic:
    """Small per-call evidence writer; failure to diagnose never becomes authority."""

    def __init__(
        self, component: str, *, directory: Path | None = None,
        workspace: Path | None = None, diagnostic_id: str = "",
        identities: dict[str, str] | None = None,
    ):
        self.id = diagnostic_id if _ID.fullmatch(diagnostic_id) else f"diag-{uuid.uuid4().hex}"
        self.component = component if component in {"daemon", "repo_map", "workflow"} else "unknown"
        self.phase = "requested"
        self.last_completed_phase = "requested"
        self.primary_phase = ""
        self.saved = False
        self.primary_type = ""
        self._records = 0
        self._omitted = 0
        self._lock = Lock()
        self._primary_saved: bool | None = None
        self._recorder: TraceRecorder | None = None
        self.path: Path | None = None
        self.identities = {
            key: correlation(value) for key, value in (identities or {}).items()
            if key in {"session_id", "interaction_id", "call_id", "run_id", "workspace_id"}
            and isinstance(value, str)
        }
        try:
            if directory is None:
                directory = prepare_user_storage(workspace or Path.cwd()).workspace_state / "diagnostics"
            _secure_directory(directory)
            self._recorder = TraceRecorder(
                run_id=self.id, trace_id=self.id, agent_id=self.id,
                session_id="diagnostic", trace_dir=directory,
            )
            self.path = self._recorder.path
        except Exception:
            # No recursive logging or raw secondary exception propagation.
            pass

    def advance(self, phase: str, **facts: object) -> bool:
        with self._lock:
            if (not self.primary_type and self.phase != phase and phase not in {
                "deadline_exhausted", "child_exited", "identity_mismatch", "failed",
                "cleanup", "finished", "unknown",
            }):
                self.last_completed_phase = self.phase
            self.phase = phase if phase in _PHASES else "unknown"
            completed = self.last_completed_phase
        return self._write([], facts, phase=phase, completed=completed)

    def failure(self, error: BaseException) -> PublicRuntimeError:
        return self.persist(self.capture(error))

    def capture(self, error: BaseException) -> CapturedFailure:
        """Reserve first-cause identity under producer locks, with no I/O."""
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise error
        with self._lock:
            phase, completed = self.phase, self.last_completed_phase
            primary = not self.primary_type
            if primary:
                kind = type(error)
                self.primary_type = next((known.__name__ for known in _EXCEPTION_TYPES
                                          if kind is known), "Exception")
                self.primary_phase = phase
                self._primary_saved = False
        return CapturedFailure(error, phase, completed, primary)

    def persist(self, captured: CapturedFailure) -> PublicRuntimeError:
        """Extract/write after producer locks are released; keep the captured phase."""
        try:
            evidence = exception_evidence(captured.error)
        except Exception:
            evidence = [{"type": self.primary_type, "frames": [], "capture_failed": True}]
        if captured.primary:
            with self._lock:
                self.primary_type = evidence[0]["type"]
        saved = self._write(evidence, {"primary_record": captured.primary},
                            phase=captured.phase, completed=captured.completed)
        if captured.primary:
            with self._lock:
                self._primary_saved = saved
        return self.public_failure()

    def public_failure(self) -> PublicRuntimeError:
        evidence_saved = self.saved and self._primary_saved is not False
        saved = "saved" if evidence_saved else "unavailable"
        phase = self.primary_phase or self.phase
        return PublicRuntimeError(PublicError(
            f"{self.component}_failed",
            f"{self.component} failed ({phase}); diagnostic={self.id}; evidence={saved}.",
            metadata={"diagnostic_id": self.id, "evidence_saved": evidence_saved},
        ))

    def cleanup_error(self, error: BaseException) -> None:
        with self._lock:
            self.phase = "cleanup"
            completed = self.last_completed_phase
        try:
            evidence = exception_evidence(error)
        except Exception:
            evidence = [{"type": _exception_identity(error)[0], "frames": [], "capture_failed": True}]
        self._write(evidence, {}, phase="cleanup", completed=completed)

    def _write(self, exceptions: list[dict], facts: dict, *, phase: str, completed: str) -> bool:
        with self._lock:
            # Ordinary events cannot consume error capacity, and secondary errors
            # cannot consume the last slot while a first cause awaits persistence.
            limit = (64 if facts.get("primary_record") or self._primary_saved is True
                     else 63) if exceptions else 60
            if self._recorder is None or self._records >= limit:
                self._omitted += 1
                self.saved = False
                return False
            self._records += 1
            if self._omitted:
                facts = {**facts, "records_omitted": self._omitted}
            if self._records == limit:
                facts = {**facts, "record_limit_reached": True}
            payload = {
            "schema": SCHEMA, "id": self.id, "component": self.component,
            "phase": phase if phase in _PHASES else "unknown", "primary_type": self.primary_type,
            "primary_phase": self.primary_phase,
            "last_completed_phase": completed,
            "exceptions": exceptions, "version": __version__,
            "identities": self.identities,
            "facts": {key: value for key, value in facts.items()
                      if key in _FACTS and (type(value) in (bool, int) or value is None)},
            }
        try:
            previous = self._recorder.dropped_events
            self._recorder.log("operation_diagnostic", operation=payload)
            saved = self._recorder.enabled and self._recorder.dropped_events == previous
        except Exception:
            saved = False
        with self._lock:
            self.saved = saved
        return saved
