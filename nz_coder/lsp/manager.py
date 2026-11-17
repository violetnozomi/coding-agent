"""工作区语言服务的唯一所有者：登记、启动、等待和结清。"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid

from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.foundation.workspace_trust import ConfigSnapshot
from nz_coder.protocol.public_error import to_public_error
from nz_coder.runtime.core.run_settings import current_run_settings

from .client import LSPClient, LSPError, LSPCancelledError, LSPTimeoutError
from .servers import ResolvedServer, resolve_server
from .types import LspOperation

_LOCK = threading.RLock()


@dataclass
class _Entry:
    key: tuple
    resolved: ResolvedServer
    source: Path
    workspace: Path
    config: ConfigSnapshot
    context: contextvars.Context
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "candidate"
    client: LSPClient | None = None
    worker: threading.Thread | None = None
    scheduled: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    closing: threading.Event = field(default_factory=threading.Event)
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    lease_id: str = ""
    error: str = ""
    reason_code: str = ""
    startup_operation: LspOperation | None = None


_ENTRIES: dict[tuple, _Entry] = {}
_RETIRED_WORKSPACES: set[str] = set()


def _check_operation(operation):
    if operation.cancelled():
        raise LSPCancelledError("LSP operation cancelled")
    if operation.remaining() <= 0:
        raise LSPTimeoutError("LSP operation deadline exceeded")


def _configuration(workspace, config_snapshot):
    from nz_coder.foundation.workspace_trust import (
        active_config_snapshot,
        current_config_snapshot,
    )

    active = active_config_snapshot(workspace)
    return config_snapshot or active or current_config_snapshot(
        workspace
    ), config_snapshot is None and active is None


def _key(resolved, workspace, snapshot, legacy=False):
    settings = current_run_settings()
    timeouts = (
        (
            settings.lsp_initialize_timeout,
            settings.lsp_diagnostic_wait,
            settings.lsp_request_timeout,
        )
        if legacy
        else (
            snapshot.get_float(
                "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", 20, minimum=0.001, maximum=600
            ),
            snapshot.get_float(
                "NZ_LSP_DIAGNOSTIC_WAIT_SECONDS", 2, minimum=0, maximum=600
            ),
            snapshot.get_float(
                "NZ_LSP_REQUEST_TIMEOUT_SECONDS", 10, minimum=0.001, maximum=600
            ),
        )
    )
    return (
        str(workspace.resolve()),
        resolved.server_id,
        str(resolved.root.resolve()),
        resolved.fingerprint,
        resolved.command,
        resolved.config_source,
        resolved.environment_fingerprint,
        resolved.initialization_json,
        resolved.settings_json,
        snapshot.control_fingerprint,
        hashlib.sha256(str(resolved.trust_store_path or "").encode()).hexdigest(),
        resolved.trust_record_key,
        *map(str, timeouts),
    )


def _client_key(path, workspace, config_snapshot=None):
    snapshot, legacy = _configuration(workspace, config_snapshot)
    resolved = resolve_server(path, workspace, config_snapshot=snapshot)
    return (
        (resolved, _key(resolved, workspace, snapshot, legacy))
        if resolved
        else (None, None)
    )


def _entry_allowed(entry, workspace):
    workspace = Path(workspace).resolve()
    if entry.workspace == workspace:
        return True
    # 仅使用私有准备环境捕获的来源，不接受控制请求补充任意父目录。
    for path, device, inode in entry.resolved.origin_workspaces:
        origin = Path(path)
        if origin != workspace:
            continue
        try:
            info = origin.stat()
        except OSError:
            return False
        return (info.st_dev, info.st_ino) == (device, inode)
    return False


def _initialize(entry):
    try:
        if entry.closing.is_set():
            return
        entry.client.start(entry.startup_operation)
        if entry.client._closed or entry.client.process.poll() is not None:
            raise LSPError("Language server exited during initialization")
        with _LOCK:
            if not entry.closing.is_set():
                entry.state = "ready"
    except Exception as exc:
        try:
            entry.client.close(force=True)
        except Exception:
            entry.reason_code = "cleanup_pending"
        with _LOCK:
            entry.error = to_public_error(exc).message
            if entry.reason_code != "cleanup_pending":
                entry.reason_code = "lsp_initialization_failed"
            if not entry.closing.is_set():
                entry.state = (
                    "cleanup_pending"
                    if entry.reason_code == "cleanup_pending"
                    else "failed"
                )
    finally:
        entry.done.set()


def _begin(entry):
    profile = entry.resolved
    entry.client = LSPClient(
        server_id=profile.server_id,
        command=profile.command,
        root=profile.root,
        language_id=profile.language_id,
        analysis_paths=profile.analysis_paths,
        initialize_timeout=float(entry.key[-3]),
        diagnostic_wait=float(entry.key[-2]),
        request_timeout=float(entry.key[-1]),
        execution_identity=profile.execution_identity,
        environment=profile.environment,
        initialization_json=profile.initialization_json,
        settings_json=profile.settings_json,
        analysis_environment=profile.analysis_environment,
        analysis_profile=profile,
        analysis_workspace=entry.workspace,
    )
    from nz_coder.state.sessions import active_session_id

    session = active_session_id() or "lsp-cache"
    lease = capability_leases().create(
        kind="lsp-client",
        resource_id=entry.entry_id,
        workspace=entry.workspace,
        control_fingerprint=entry.config.control_fingerprint,
        run_id=session,
        interaction_id=session,
        owner_session=session,
        revoke=lambda: _close_entry(entry, force=True),
    )
    entry.lease_id = lease.lease_id
    entry.state = "starting"
    entry.startup_operation = LspOperation(
        entry.closing, time.monotonic() + float(entry.key[-3])
    )
    entry.worker = threading.Thread(
        target=lambda: entry.context.copy().run(_initialize, entry),
        name=f"nzcoder-lsp-init-{profile.server_id}",
        daemon=True,
    )
    # start 与登记在同一锁区间，close 不会把尚未调度的 worker 当成已退出。
    entry.scheduled = True
    try:
        entry.worker.start()
    except Exception:
        entry.state = "failed"
        entry.done.set()
        entry.scheduled = False
        raise


def get_client_for_file(
    path: Path,
    workspace: Path,
    *,
    config_snapshot: ConfigSnapshot | None = None,
    operation: LspOperation | None = None,
) -> LSPClient | None:
    workspace = Path(workspace).resolve()
    snapshot, legacy = _configuration(workspace, config_snapshot)
    enabled = (
        current_run_settings().lsp_enabled
        if legacy
        else snapshot.get_bool("NZ_LSP_ENABLED", True)
    )
    if not enabled:
        # 调用级禁用不是workspace owner退出，不能借此设置不可逆退休位。
        return None
    operation = operation or LspOperation(
        None,
        time.monotonic()
        + current_run_settings().lsp_initialize_timeout
        + current_run_settings().lsp_request_timeout,
    )
    _check_operation(operation)
    resolved = resolve_server(path, workspace, config_snapshot=snapshot)
    if resolved is None:
        return None
    key = _key(resolved, workspace, snapshot, legacy)
    with _LOCK:
        if str(workspace) in _RETIRED_WORKSPACES:
            return None
        entry = _ENTRIES.get(key)
        if entry is None:
            entry = _Entry(
                key,
                resolved,
                Path(path),
                workspace,
                snapshot,
                contextvars.copy_context(),
            )
            _ENTRIES[key] = entry
        if not resolved.trusted:
            entry.reason_code = "lsp_trust_required"
            entry.error = "LSP executable requires approval through /lsp trust ENTRY_ID"
            return None
        if entry.closing.is_set():
            raise LSPError("LSP cleanup pending")
        if entry.state == "candidate":
            _begin(entry)
    while not entry.done.is_set():
        _check_operation(operation)
        entry.done.wait(min(0.02, operation.remaining()))
    _check_operation(operation)
    with _LOCK:
        _observe_exit(entry)
    if entry.closing.is_set():
        return None
    if entry.state == "failed":
        if (
            entry.reason_code in {"lsp_server_exited", "lsp_connection_closed"}
            and entry.client.process.poll() is not None
        ):
            _close_entry(entry, force=True, deadline=operation.deadline)
            return _get_replacement(path, workspace, snapshot, operation)
        return None
    if entry.state != "ready":
        return None
    return entry.client


def _get_replacement(path, workspace, snapshot, operation):
    resolved = resolve_server(path, workspace, config_snapshot=snapshot)
    if resolved is None or not resolved.trusted:
        return None
    key = _key(resolved, workspace, snapshot)
    with _LOCK:
        if str(Path(workspace).resolve()) in _RETIRED_WORKSPACES:
            return None
        entry = _ENTRIES.get(key)
        if entry is None:
            entry = _Entry(
                key,
                resolved,
                Path(path),
                workspace,
                snapshot,
                contextvars.copy_context(),
            )
            _ENTRIES[key] = entry
            _begin(entry)
    while not entry.done.is_set():
        _check_operation(operation)
        entry.done.wait(min(0.02, operation.remaining()))
    return (
        entry.client if entry.state == "ready" and not entry.closing.is_set() else None
    )


def prewarm_client_for_file(path, workspace, *, config_snapshot=None):
    """仅调度同一 entry 的启动，不同步文件、不新增预热线程。"""
    snapshot, legacy = _configuration(workspace, config_snapshot)
    enabled = (
        current_run_settings().lsp_enabled
        if legacy
        else snapshot.get_bool("NZ_LSP_ENABLED", True)
    )
    if not enabled:
        return
    resolved = resolve_server(path, workspace, config_snapshot=snapshot)
    if resolved is None:
        return
    key = _key(resolved, workspace, snapshot, legacy)
    with _LOCK:
        if str(Path(workspace).resolve()) in _RETIRED_WORKSPACES:
            return
        if key not in _ENTRIES:
            entry = _Entry(
                key,
                resolved,
                Path(path),
                Path(workspace).resolve(),
                snapshot,
                contextvars.copy_context(),
            )
            _ENTRIES[key] = entry
            if resolved.trusted:
                _begin(entry)
            else:
                entry.reason_code = "lsp_trust_required"


def _close_entry(entry, *, force=False, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 6
    entry.closing.set()
    with _LOCK:
        entry.state = "closing"
    if not entry.close_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise LSPError("LSP cleanup pending")
    try:
        if entry.client is not None:
            entry.client.close(
                force=force or not entry.done.is_set(), deadline=deadline
            )
        if entry.worker is not None and entry.scheduled:
            if entry.worker is threading.current_thread():
                raise LSPError("LSP cleanup pending")
            entry.worker.join(timeout=max(0, deadline - time.monotonic()))
            if entry.worker.is_alive() or not entry.done.is_set():
                raise LSPError("LSP cleanup pending")
        with _LOCK:
            if _ENTRIES.get(entry.key) is entry:
                del _ENTRIES[entry.key]
            if entry.lease_id:
                capability_leases().release(entry.lease_id)
                entry.lease_id = ""
    except Exception:
        with _LOCK:
            entry.state = "cleanup_pending"
            entry.reason_code = "cleanup_pending"
        raise
    finally:
        entry.close_lock.release()


def client_startup_error(path, workspace, *, config_snapshot=None):
    _, key = _client_key(path, workspace, config_snapshot)
    with _LOCK:
        entry = _ENTRIES.get(key)
        return entry.error if entry is not None else ""


def _observe_exit(entry):
    if entry.state != "ready":
        return
    if entry.client.process.poll() is not None:
        entry.state = "failed"
        entry.reason_code = "lsp_server_exited"
    elif entry.client._closed:
        entry.state = "failed"
        entry.reason_code = "lsp_connection_closed"


def client_status_summary(workspace):
    with _LOCK:
        rows = []
        for entry in _ENTRIES.values():
            if not _entry_allowed(entry, workspace):
                continue
            _observe_exit(entry)
            profile = entry.resolved
            state = (
                "trust-required"
                if entry.reason_code == "lsp_trust_required"
                else entry.state
            )
            rows.append(
                {
                    "entry_id": entry.entry_id,
                    "id": profile.server_id,
                    "root": str(profile.root),
                    "status": state,
                    "reason_code": entry.reason_code,
                    "analysis_environment": profile.analysis_environment,
                    "analysis_configuration_source": profile.analysis_configuration_source,
                    "execution_identity": profile.execution_identity.public()
                    if profile.execution_identity
                    else {"fingerprint": profile.fingerprint},
                    "source": profile.source,
                }
            )
        return sorted(rows, key=lambda row: (row["id"], row["root"], row["entry_id"]))


def control_client(workspace, entry_id, action):
    """仅实际 owner 可按不透明 ID 控制已捕获的 profile。"""
    if action not in {"trust", "untrust", "restart"}:
        raise ValueError("Unsupported LSP control action")
    with _LOCK:
        entry = next(
            (item for item in _ENTRIES.values() if item.entry_id == entry_id), None
        )
        if entry is None:
            return {"status": "no_active_service"}
        if not _entry_allowed(entry, workspace):
            raise ValueError("LSP entry belongs to a different workspace")

    def revalidate():
        current = resolve_server(
            entry.source, entry.workspace, config_snapshot=entry.config
        )
        if current is None or _key(current, entry.workspace, entry.config) != entry.key:
            raise ValueError("LSP entry is stale; query current status again")
        return current

    profile = (
        entry.resolved if action == "untrust" else entry.context.copy().run(revalidate)
    )
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
    from .servers import server_trust_kind

    store = WorkspaceTrustStore(profile.trust_store_path)
    executable = (
        str(profile.execution_identity.executable_path)
        if profile.execution_identity is not None
        else ""
    )
    kind = server_trust_kind(entry.source, profile)
    if action == "untrust":
        try:
            if profile.source == "workspace":
                store.remove_record(profile.trust_record_key)
        finally:
            # 撤销针对已捕获的授权范围；载荷变更不能阻止回收旧资源。
            with _LOCK:
                matching = [
                    item
                    for item in _ENTRIES.values()
                    if item.workspace == entry.workspace
                    and server_trust_kind(item.source, item.resolved) == kind
                    and item.resolved.trust_record_key == profile.trust_record_key
                    and item.resolved.trust_store_path == profile.trust_store_path
                ]
            _close_selected(matching)
        return {
            "status": "untrusted" if profile.source == "workspace" else "closed",
            "entry_id": entry_id,
        }
    if profile.source == "workspace" and action == "trust":
        # 写入用户实际查看的指纹；替换载荷不会获得旧批准。
        store.trust(entry.workspace, kind, profile.fingerprint, executable=executable)
    _close_entry(entry)

    def reopen():
        return get_client_for_file(
            entry.source, entry.workspace, config_snapshot=entry.config
        )

    replacement = entry.context.copy().run(reopen)
    with _LOCK:
        current = next(
            (
                item
                for item in _ENTRIES.values()
                if item.client is replacement and replacement is not None
            ),
            None,
        )
    return {
        "status": "ready" if replacement else "unavailable",
        "entry_id": current.entry_id if current else "",
    }


def activate_workspace_clients(workspace):
    """仅由实际工作区owner重新取得租约时解除退休，普通查询无此权限。"""
    owner = str(Path(workspace).resolve())
    with _LOCK:
        if any(
            str(entry.workspace) == owner and entry.closing.is_set()
            for entry in _ENTRIES.values()
        ):
            raise LSPError("LSP cleanup pending")
        _RETIRED_WORKSPACES.discard(owner)


def retire_workspace_clients(workspace):
    with _LOCK:
        _RETIRED_WORKSPACES.add(str(Path(workspace).resolve()))
        for entry in _ENTRIES.values():
            if entry.workspace == Path(workspace).resolve():
                entry.closing.set()


def _close_selected(entries):
    errors = []
    deadline = time.monotonic() + 6
    for entry in entries:
        entry.closing.set()
    for entry in entries:
        try:
            _close_entry(entry, deadline=deadline)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise LSPError("LSP cleanup pending") from errors[0]


def close_workspace_clients(workspace):
    retire_workspace_clients(workspace)
    with _LOCK:
        entries = [
            entry
            for entry in _ENTRIES.values()
            if entry.workspace == Path(workspace).resolve()
        ]
    _close_selected(entries)


def close_all_clients():
    with _LOCK:
        entries = list(_ENTRIES.values())
        _RETIRED_WORKSPACES.update(str(entry.workspace) for entry in entries)
    _close_selected(entries)


atexit.register(close_all_clients)
