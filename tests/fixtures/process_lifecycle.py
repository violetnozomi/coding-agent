"""可控的外部 I/O 目标；不模拟服务内部状态。"""

from __future__ import annotations

import subprocess
import threading
import time


class ControlledBackend:
    pid = 71001
    tty = True
    pipefail = False
    lifecycle_mode = "controlled"

    def __init__(self):
        self.body_exit = threading.Event()
        self.eof = threading.Event()
        self.read_entered = threading.Event()
        self.io_entered = threading.Event()
        self.io_release = threading.Event()
        self.io_release.set()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()
        self.stop_seen = threading.Event()
        self.control_fault = threading.Event()
        self.close_fault = threading.Event()
        self.auto_stop = False
        self.exit_code = 0
        self.tail = b""
        self.controls = []
        self.closed = False
        self.close_attempts = 0

    def read_bytes(self, size):
        self.read_entered.set()
        self.eof.wait()
        tail, self.tail = self.tail, b""
        return tail

    def write_bytes(self, data):
        self.io_entered.set()
        self.io_release.wait()

    def resize(self, *, rows, cols):
        self.io_entered.set()
        self.io_release.wait()

    def poll(self):
        self.controls.append(("poll", threading.get_ident()))
        if self.control_fault.is_set():
            raise OSError("poll unavailable")
        return self.exit_code if self.body_exit.is_set() else None

    def wait(self, timeout=None):
        self.controls.append(("wait", threading.get_ident()))
        if self.control_fault.is_set():
            raise OSError("wait unavailable")
        if not self.body_exit.wait(timeout):
            raise subprocess.TimeoutExpired("controlled", timeout)
        return self.exit_code

    def terminate_tree(self, *, grace_seconds):
        self.controls.append(("terminate", threading.get_ident()))
        self.stop_seen.set()
        if self.auto_stop:
            self.body_exit.set()
            self.eof.set()

    def close(self):
        self.controls.append(("close", threading.get_ident()))
        self.close_attempts += 1
        self.close_entered.set()
        self.close_release.wait()
        if self.close_fault.is_set():
            raise OSError("close unavailable")
        self.closed = True

    def recover(self):
        self.control_fault.clear()
        self.close_fault.clear()
        self.io_release.set()
        self.close_release.set()
        self.body_exit.set()
        self.eof.set()


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


def make_preparation_scope(workspace, *, child="child", timeout=5.0,
                           cancel_event=None, settings=None, on_target=None, deadline=None, settlement_id=None):
    from dataclasses import replace
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.process.preparation import PreparationScope

    return PreparationScope(
        workspace, parent_session_id="parent", child_session_id=child,
        settlement_id=settlement_id if settlement_id is not None else "settlement-" + child, config_snapshot=None,
        run_settings=settings or replace(RunSettings.from_legacy_globals(), process_kill_grace=.02),
        cancel_event=cancel_event if cancel_event is not None else threading.Event(),
        deadline=deadline if deadline is not None else time.monotonic() + timeout,
        on_target=on_target if on_target is not None else lambda worktree: None,
    )


def persist_preparing_worktree(workspace, *, parent="parent", mode="git", workflow_run_id=""):
    from dataclasses import asdict
    from pathlib import Path
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.worktree import Worktree, WorktreeManager

    target = WorktreeManager(workspace).worktree_dir / "child"
    target.mkdir(parents=True)
    (target / "partial.txt").write_text("keep partial data")
    state = subagent._new_subagent_state(parent, "explore", ["bash"])
    state.update(
        session_id="child", settlement_id="preparation-child", status="error",
        workflow_run_id=workflow_run_id, read_only=True, changed_files=[],
        finished_at=time.time() - 10 * 3600, worktree_preparing=True,
        worktree=asdict(Worktree("child", str(target), "subagent-child" if mode == "git" else "", "HEAD", "", mode=mode)),
    )
    subagent._save_subagent_state(parent, state, Path(workspace))
    return state


def make_command_authority(tmp_path, child='child', *, max_requests=4, read_only=False, policy=None):
    from dataclasses import replace
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.core.execution_context import CommandExecutionPolicy
    from nz_coder.runtime.process.command_broker import CommandAuthority, command_directory_identity

    authority = CommandAuthority(
        workspace=tmp_path, execution_root=tmp_path,
        execution_identity=command_directory_identity(tmp_path),
        parent_session_id='parent', child_session_id=child,
        settlement_id='settlement-' + child, agent_type='explore' if read_only else 'general',
        read_only=read_only, allowed_tools=frozenset({'bash', 'process'}),
        config_snapshot=None,
        run_settings=replace(RunSettings.from_legacy_globals(), max_parallel_tasks=max_requests, process_kill_grace=0.02),
        execution_policy=policy or CommandExecutionPolicy(False, 'initial'),
    )
    return authority


def make_command_scope(tmp_path, child='child', *, max_requests=4, read_only=False, ready=True, errors=None, policy=None, publisher=None, deadline=None, allowed_tools=None, command_authorizer=None):
    from nz_coder.runtime.process.command_broker import ParentCommandScope

    authority = make_command_authority(tmp_path, child, max_requests=max_requests, read_only=read_only, policy=policy)
    if allowed_tools is not None:
        from dataclasses import replace
        authority = replace(authority, allowed_tools=frozenset(allowed_tools))
    scope = ParentCommandScope(authority, deadline=deadline if deadline is not None else time.monotonic()+5, child_publisher=publisher, on_channel_error=errors.append if errors is not None else lambda e: None, command_authorizer=command_authorizer)
    if ready:
        scope.policy_ready(scope.authority.execution_policy.fingerprint)
    return scope


def make_auxiliary_runner(workspace, *, child="child", timeout=5.0, settings=None, cancel_event=None):
    from dataclasses import replace
    from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
    from nz_coder.runtime.process.auxiliary import AuxiliaryCommandRunner

    authority = make_command_authority(workspace, child)
    if settings is not None:
        authority = replace(authority, run_settings=settings)
    return AuxiliaryCommandRunner(
        authority, request_id=1, deadline=time.monotonic() + timeout,
        cancel_event=cancel_event if cancel_event is not None else threading.Event(),
        environment=build_sanitized_subprocess_env(profile="workspace-command"),
    )


def complete_prepared_evidence(prepared, state, preparation):
    from dataclasses import replace
    from nz_coder.runtime.agent.child_contracts import resolve_effective_evidence_refs
    from nz_coder.runtime.agent.prepared_evidence import capture_diff_evidence

    refs = resolve_effective_evidence_refs(None, state.get("evidence_refs"))
    state["evidence_refs"] = refs
    snapshot = capture_diff_evidence(
        refs, workspace=prepared.parent_workspace,
        parent_session_id=preparation.parent_session_id, child_session_id=state["session_id"],
        settlement_id=state["settlement_id"], preparation=preparation,
    )
    return replace(prepared, diff_evidence=snapshot)


def make_command_authorizer(workspace, *, asker=None):
    """正向进程生命周期用例显式模拟宿主已委托的自动权限域。"""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.command_authorization import CommandAuthorizer

    with scoped_workdir(workspace):
        return CommandAuthorizer(PermissionManager("auto", asker=asker)).fork(workspace)
