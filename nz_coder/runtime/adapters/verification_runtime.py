"""运行验证适配器：采集宿主事实，域外执行 I/O，域内统一提交。"""
from __future__ import annotations

import argparse
import fnmatch
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity, commands_equivalent, observed_check_subject
from nz_coder.runtime.verification.domain.models import (
    ExecutionFact, ExecutionReceipt, RunScope, TaskDefinition, TaskScope, WorkspaceObservation, WorkspaceVersion,
    is_check_execution,
)
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.verification.domain.rules import observed_check_stage
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from .verification_facts import bind_execution_facts, command_capabilities, command_receipt, is_host_verification_tool
from .verification_plans import check_spec, register_verification_plan
from .verification_workspace import (
    collect_artifact_facts, collect_workspace_settlement, mutation_path_identities,
    unaccounted_workspace_changes,
)


def _restore_verification_domain(state, *, session_id, workspace, interaction_run_id, agent_id, lock, version, require_targeted, messages=None):
    """预检和激活使用相同的身份及检查点验证。"""
    saved = getattr(state, "_restored_verification_domain", None)
    saved_scope = saved.get("scope") if isinstance(saved, dict) else None
    saved_task = saved_scope.get("task") if isinstance(saved_scope, dict) else None
    saved_task = saved_task if isinstance(saved_task, dict) else {}
    lineage = str(saved_task.get("task_lineage_id") or "")
    scope = RunScope(TaskScope(session_id, lineage or uuid4().hex, str(Path(workspace).resolve())), interaction_run_id, agent_id)
    # 先完整验证旧数据，再重建来源；缺正文不能用旧 source/required 自证权限。
    from nz_coder.runtime.verification.domain.checkpoint import AuthoritySourceUnavailable
    try:
        domain = restore_domain(saved, lock=lock, scope=scope, version=version, require_targeted=require_targeted)
    except AuthoritySourceUnavailable:
        contract = _recover_user_contract(state, messages, session_id=session_id, workspace=workspace)
        authority = TaskDefinition(scope.task, state.task_authority_epoch, state.task_authority_message_id, contract)
        domain = restore_domain(saved, lock=lock, scope=scope, version=version,
                                require_targeted=require_targeted, authority=authority)
    if saved is None and (state.task_contract or state.verification_definition):
        contract = _recover_user_contract(state, messages, session_id=session_id, workspace=workspace)
        domain.declare_task(TaskDefinition(scope.task, state.task_authority_epoch, state.task_authority_message_id, contract))
    elif not isinstance(saved, dict):
        return domain
    if state.task_authority_epoch > domain.task_definition.authority_revision:
        from nz_coder.protocol.message_schema import is_synthetic_user_message
        followup = next((m for m in (messages or []) if isinstance(m, dict) and m.get("role") == "user"
            and not is_synthetic_user_message(m) and m.get("_nz_message_id") == state.task_authority_message_id), None)
        if followup is None:
            raise AuthoritySourceUnavailable()
        from nz_coder.runtime.adapters.context import new_runtime_state
        from nz_coder.runtime.agent.task_contract import parse_task_contract
        rebuilt = new_runtime_state()
        rebuilt.initial_task_text = state.initial_task_text
        rebuilt.task_contract = domain.task_definition.contract.to_dict() if domain.task_definition.contract else {}
        rebuilt.apply_current_round_instruction(followup["content"], workspace=workspace, capture_task_references=False)
        domain.declare_task(TaskDefinition(scope.task, state.task_authority_epoch, state.task_authority_message_id,
                                          parse_task_contract(rebuilt.task_contract, workspace=workspace)))
    # 域内定义才是已持久化权威，外层 RuntimeState 字段只是投影。
    contract = domain.task_definition.contract
    state.task_contract = contract.to_dict() if contract is not None else {}
    from nz_coder.runtime.agent.task_policy import test_command_targets
    first = contract.acceptance_commands[0] if contract and contract.acceptance_commands else ""
    state.verification_definition = {"command": first, "targets": list(test_command_targets(first))} if first else {}
    return domain


def _recover_user_contract(state, messages, *, session_id, workspace):
    from nz_coder.protocol.message_schema import COMPACTION_KEY, SUMMARY_KEY, SESSION_SUMMARY_KEY, is_synthetic_user_message
    from nz_coder.runtime.adapters.context import new_runtime_state
    from nz_coder.runtime.verification.domain.checkpoint import AuthoritySourceUnavailable
    if messages is None:
        from nz_coder.state.sessions import load_session
        from nz_coder.runtime.process.workdir import scoped_workdir
        with scoped_workdir(workspace):
            messages = load_session(session_id).get("messages", [])
    if not isinstance(messages, list) or any(isinstance(m, dict) and any(
            m.get(key) for key in (COMPACTION_KEY, SUMMARY_KEY, SESSION_SUMMARY_KEY)) for m in messages):
        raise AuthoritySourceUnavailable()
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"
             and not is_synthetic_user_message(m) and isinstance(m.get("content"), str)]
    from nz_coder.runtime.core.execution_context import explicit_replacement_task
    bodies = [explicit_replacement_task(m) or m["content"].strip() for m in users]
    starts = [i for i, text in enumerate(bodies) if text == state.initial_task_text.strip()]
    if len(starts) != 1:
        raise AuthoritySourceUnavailable()
    users = users[starts[0]:]
    if any(explicit_replacement_task(m) is not None for m in users[1:]):
        raise AuthoritySourceUnavailable()
    identities = {m.get("_nz_message_id") for m in users if m.get("_nz_message_id")}
    required_ids = {ref.get("source_message_id") for ref in state.task_reference_evidence if ref.get("source_message_id")}
    if (any(not m.get("_nz_message_id") for m in users)
            or state.task_authority_message_id and state.task_authority_message_id not in identities
            or not required_ids.issubset(identities)
            or len(users) < max(1, state.task_authority_epoch)):
        raise AuthoritySourceUnavailable()
    rebuilt = new_runtime_state()
    initial = explicit_replacement_task(users[0]) or users[0]["content"]
    rebuilt.initial_task_text = initial
    rebuilt.set_acceptance_criteria_from_text(initial, workspace=workspace)
    for message in users[1:]:
        rebuilt.apply_current_round_instruction(message["content"], workspace=workspace,
            source_message_id=message["_nz_message_id"], capture_task_references=False)
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    return parse_task_contract(rebuilt.task_contract, workspace=workspace)


def preflight_runtime_restore(*, session_id, workspace, activation, settings):
    """只读预检本次激活实际会读取的旧运行状态。"""
    import threading
    from nz_coder.runtime.core.execution_context import explicit_replacement_task, is_genuine_runtime_followup
    from nz_coder.runtime.adapters.context import new_runtime_state
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import session_runtime_state_path

    if not settings.runtime_state_persist or explicit_replacement_task(activation.current_user_message) is not None:
        return
    state = new_runtime_state()
    allow_inactive = activation.resume_activation or is_genuine_runtime_followup(activation.current_user_message)
    with scoped_workdir(workspace):
        path = session_runtime_state_path(session_id)
        if not state.load(path, allow_inactive=allow_inactive):
            return
    _restore_verification_domain(state, session_id=session_id, workspace=workspace,
        interaction_run_id="preflight", agent_id="preflight", lock=threading.RLock(),
        version=WorkspaceVersion(), require_targeted=False)


def _generic_test_covers_artifact(path: Path, command: str, cwd: str) -> bool:
    """仅为已读材料筛选候选文件，不参与正式需求满足或验收认证。"""
    base = Path(cwd).resolve()
    try:
        relative = path.resolve().relative_to(base)
    except ValueError:
        return False
    identity = command_identity(command, cwd)
    argv = identity.argv
    if identity.opaque or not argv:
        return False
    executable = Path(argv[0]).name
    from nz_coder.runtime.agent.task_policy import (
        _python_entry, _test_selections, unittest_selectors, unittest_selector_matches_path,
    )

    entry = _python_entry(argv)
    pytest = executable in {"pytest", "py.test", "pytest3"} or bool(
        entry is not None and entry[2] and entry[0] in {"pytest", "py.test"})
    selectors = unittest_selectors(argv)
    unittest = selectors is not None
    go = executable == "go" and argv[1:2] == ("test",)
    node = executable in {"node", "nodejs"} and argv[1:2] == ("--test",)
    if not (pytest or unittest or go or node):
        return False
    selections = _test_selections(command)
    if not selections or any(item.kind == "unknown" for item in selections):
        return False
    runner_args = entry[1] if entry is not None else list(argv[1:])
    pytest_args = runner_args
    # 排除/筛选与仅收集会改变执行集合；无法由文件路径证明时保留观察，不认领需求。
    if pytest and any(token.split("=", 1)[0] in {
        "--ignore", "--ignore-glob", "--deselect", "--pyargs", "--collect-only", "--co",
    } or token.startswith(("-k", "-m")) for token in pytest_args):
        return False
    if unittest and any(token.startswith("-k") for token in runner_args):
        return False
    if go and any(token.split("=", 1)[0] in {"-run", "-skip", "-list", "-tags", "-short", "-args"} for token in argv[2:]):
        return False
    from .verification_plans import _go_test_covers_artifact

    if go and relative.suffix == ".go":
        return _go_test_covers_artifact(PurePosixPath(relative.as_posix()), command)
    if unittest and selectors:
        return any(unittest_selector_matches_path(selector, relative.as_posix()) for selector in selectors)
    targets = tuple(target for item in selections for target in item.targets)
    if targets:
        return any(path.resolve() == (base / target.split("::", 1)[0]).resolve()
                   or path.resolve().is_relative_to((base / target.split("::", 1)[0]).resolve()) for target in targets)
    if relative.suffix != ".py":
        return False
    if pytest:
        return True
    if not unittest:
        return False
    args = list(runner_args)
    if not args or args[0] == "discover":
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("positional", nargs="*")
        parser.add_argument("-s", "--start-directory")
        parser.add_argument("-p", "--pattern")
        parser.add_argument("-t", "--top-level-directory")
        parser.add_argument("-k")
        for short, long in (("-v", "--verbose"), ("-q", "--quiet"), ("-b", "--buffer"),
                            ("-f", "--failfast"), ("-c", "--catch")):
            parser.add_argument(short, long, action="store_true")
        parser.add_argument("--locals", action="store_true")
        try:
            options, unknown = parser.parse_known_args(args[1:])
        except argparse.ArgumentError:
            return False
        if unknown or len(options.positional) > 3:
            return False
        start = options.start_directory or (options.positional[0] if options.positional else ".")
        pattern = options.pattern or (options.positional[1] if len(options.positional) > 1 else "test*.py")
        return path.resolve().is_relative_to((base / start).resolve()) and fnmatch.fnmatchcase(path.name, pattern)
    module = relative.with_suffix("").as_posix().replace("/", ".")
    return any(arg == relative.as_posix() or arg == module or arg.startswith(module + ".") for arg in args)


@dataclass
class ToolVerificationStart:
    """执行中归属令牌；只采集范围和调用身份，不保存验证结论。"""

    runtime: VerificationRuntime
    execution_id: str
    version: WorkspaceVersion
    revision: int
    check_ids: tuple[str, ...]
    before_reference: str | None = None
    origin: str = "model"
    zone: str = ""
    process_start_receipt: ExecutionReceipt | None = None


_TOOL_START: ContextVar[ToolVerificationStart | None] = ContextVar("nz_tool_verification_start", default=None)
_INVOCATION: ContextVar[tuple[str, str]] = ContextVar("nz_verification_invocation", default=("model", ""))


def current_verification_origin() -> str:
    """读取宿主调用来源；模型参数和工具公开元数据不能声明该身份。"""
    return _INVOCATION.get()[0]


def current_tool_verification() -> ToolVerificationStart | None:
    return _TOOL_START.get()


@contextmanager
def scoped_verification_invocation(origin: str, zone: str):
    marker = _INVOCATION.set((origin, zone))
    try:
        yield
    finally:
        _INVOCATION.reset(marker)


@contextmanager
def scoped_tool_verification(start: ToolVerificationStart):
    marker = _TOOL_START.set(start)
    try:
        yield
    finally:
        _TOOL_START.reset(marker)


class VerificationRuntime:
    def __init__(self, host, run_context):
        self.host = host
        self.run_context = run_context
        self.state = host.runtime_state
        self.store = host.workspace_snapshots
        self._pending: dict[str, ToolVerificationStart] = {}
        self._plan_signature = None
        self._unsubscribe = None
        self._local_service = None
        self._gate_prompts = 0
        self._native_runner_kind = ""

    @property
    def domain(self) -> VerificationDomain:
        domain = self.run_context.verification
        if domain is None:
            raise RuntimeError("verification domain has not initialized")
        return domain

    def version(self) -> WorkspaceVersion:
        state = self.state
        generation = state.mutation_generation
        acceptance = state.acceptance_mutation_generation
        return WorkspaceVersion(generation, state.source_mutation_generation,
            generation if acceptance is None else acceptance,
            bool(state.workspace_observation.get("complete", True)))

    def initialize(self, *, restored: bool, messages=None) -> None:
        state, context = self.state, self.run_context
        from nz_coder.runtime.core.execution_context import strict_local_tools
        if restored:
            domain = _restore_verification_domain(state, session_id=self.host.session_id, workspace=self.host.workdir,
                interaction_run_id=context.interaction_run_id, agent_id=self.host.agent_id,
                lock=context._state_lock, version=self.version(), require_targeted=strict_local_tools(), messages=messages)
        else:
            scope = RunScope(TaskScope(self.host.session_id, uuid4().hex, str(self.host.workdir.resolve())),
                             context.interaction_run_id, self.host.agent_id)
            domain = VerificationDomain(lock=context._state_lock, scope=scope,
                version=self.version(), require_targeted=strict_local_tools())
        context.attach_verification(domain)
        self.synchronize_task()
        from nz_coder.runtime.process.command_channel import current_command_client
        client = current_command_client()
        if client is not None:
            client.set_fact_receiver(self._receive_remote_terminal)
        errors = {}
        try:
            reference = self.store.verification_snapshot()
        except SnapshotError as exc:
            reference = ""
            errors["capture"] = str(exc)
        with context._state_lock:
            if not state.workspace_observation.get("reference"):
                state.workspace_observation = {"reference": reference,
                    "generation": state.mutation_generation, "complete": bool(reference),
                    "stage": "initialize", "errors": errors}
            self.domain.observe_workspace(WorkspaceObservation(self.version()))
        if errors:
            self.host.tracer.log("workspace_scope_incomplete", stage="initialize", errors=errors)
        if restored:
            for fact in collect_artifact_facts(self.domain.snapshot(), self.host.workdir, {}, state.mutation_generation):
                self.domain.observe_artifact(fact)
        self.refresh_plan()

    def synchronize_task(self) -> None:
        from nz_coder.runtime.agent.task_contract import parse_task_contract
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason:
                return
            contract = parse_task_contract(self.state.task_contract) if self.state.task_contract else None
            self.domain.declare_task(TaskDefinition(self.domain.scope.task, self.state.task_authority_epoch,
                self.state.task_authority_message_id, contract))
            declared = contract.acceptance_commands if contract is not None else ()
            user_specs = []
            for command in declared:
                if not command:
                    continue
                caps = command_capabilities(command) or ("custom_assert",)
                ids = tuple(item.id for item in contract.requirements if item.kind == "verification") if contract else ()
                covers = ()
                for capability in caps:
                    from nz_coder.runtime.agent.task_policy import test_command_targets
                    spec = check_spec(command, str(self.host.workdir), capability=capability,
                        required=True,
                        targets=tuple(test_command_targets(command)), source="user_declared",
                        requirement_ids=ids, covers=covers)
                    user_specs.append(spec)
            self.domain.replace_user_checks(tuple(user_specs))

    def _register_command(self, command: str, cwd: str, *, failed: bool = False) -> tuple[str, ...]:
        from nz_coder.intelligence.verification_planner import classify_verification_command
        from nz_coder.runtime.agent.task_policy import test_command_targets

        stage = classify_verification_command(command)
        identity = command_identity(command, cwd)
        known_specs = tuple(spec for spec in self.domain.snapshot().specs if commands_equivalent(spec.command, identity,
            native_runner_kind=spec.native_runner_kind))
        known = tuple(spec.id for spec in known_specs)
        caps = command_capabilities(command)
        if not caps:
            return known
        subject = observed_check_subject(identity)
        selected_command = subject.original if subject is not None else command
        ids = list(known)
        for capability in caps:
            selected_stage = observed_check_stage(identity, capability, stage)
            if selected_stage is None:
                continue
            requirement_ids = ()
            existing = tuple(spec for spec in known_specs if spec.capability == capability)
            if existing:
                for spec in existing:
                    self.domain.register_check(replace(spec, required=False,
                        source="failure_evidence" if failed else "model_execution", requirement_ids=requirement_ids))
                continue
            spec = check_spec(command, cwd, capability=capability, targets=tuple(test_command_targets(selected_command)),
                required=False,
                stage="static" if capability in {"syntax", "symbol", "type", "lint", "build"} else selected_stage,
                source="failure_evidence" if failed else "model_execution",
                native_runner_kind="observed_output" if subject is not None else self._native_runner_kind,
                requirement_ids=requirement_ids)
            ids.append(self.domain.register_check(spec))
        return tuple(dict.fromkeys(ids))

    def begin_tool(self, tool_call: dict) -> ToolVerificationStart:
        from nz_coder.tool_platform.execution import is_write_tool
        from nz_coder.runtime.process.command_channel import current_command_client
        self.synchronize_task()
        function = tool_call.get("function", {})
        name = str(function.get("name") or "")
        args = self.host._best_effort_tool_input(function.get("arguments", {}))
        cwd = str((self.host.workdir / str(args.get("workdir") or ".")).resolve())
        with self.run_context._state_lock:
            ids = self._register_command(str(args.get("command") or ""), cwd) if name in {"bash", "process"} and args.get("command") else ()
            # 模型可能复用 tool_call_id；每次实际派发仍是独立执行，不能复用旧事实。
            id = self.run_context.interaction_run_id + ":" + uuid4().hex
            revision = self.domain.begin_execution(id, ids, mutates=name == "bash" or is_write_tool(name),
                verifies=bool(ids) or (name not in {"bash", "process"} and is_host_verification_tool(name)))
            origin, zone = _INVOCATION.get()
            start = ToolVerificationStart(self, id, self.version(), revision, ids, origin=origin, zone=zone)
            self._pending[id] = start
        try:
            if (name == "process" and str(args.get("operation") or "").strip().lower() == "start"
                    and current_command_client() is None and self._unsubscribe is None):
                from nz_coder.runtime.process.process_service import workspace_process_service
                self._local_service = workspace_process_service(self.host.workdir)
                self._unsubscribe = self._local_service.subscribe_terminal(self._receive_local_terminal)
        except BaseException:
            # 尚未交回执行令牌，订阅失败由登记方结清，不能留下未启动的进程归属。
            with self.run_context._state_lock:
                self.domain.end_execution(start.execution_id)
                self._pending.pop(start.execution_id, None)
            raise
        return start

    def record_before(self, start: ToolVerificationStart, reference: str | None) -> None:
        start.before_reference = str(reference or "")

    def abort_tool(self, start: ToolVerificationStart, name: str, tool_input: dict) -> None:
        """排空后的异常调用核对副作用；未知进程启动仍等待真实终态。"""
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason or self.domain.execution_settled(start.execution_id):
                return
            if start.before_reference is None:
                self.domain.end_execution(start.execution_id)
                self._pending.pop(start.execution_id, None)
                return
        from nz_coder.tool_platform.execution import ToolExecutionResult, is_write_tool

        errors = {}
        try:
            after = self.store.verification_snapshot()
        except SnapshotError as exc:
            after = None
            errors["after"] = str(exc)
        result = ToolExecutionResult(name, tool_input, "", True, True, False, is_write_tool(name),
            execution_id=start.execution_id, workspace_scope={
                "workspace": str(self.host.workdir), "generation": start.version.generation,
                "before": start.before_reference, "after": after,
                "stage": "abort", "errors": errors,
            })
        # 进入 start 不证明没有创建进程，不能用无回执的内部结算提前释放归属。
        self.record_result(result, keep_execution_open=name == "process"
            and str(tool_input.get("operation") or "").strip().lower() == "start")

    def record_result(self, result, *, keep_execution_open: bool = False) -> None:
        start = self._pending.get(result.execution_id)
        if not result.executed and result.workspace_scope is None:
            if start is not None:
                with self.run_context._state_lock:
                    self.domain.end_execution(start.execution_id)
                    self._pending.pop(start.execution_id, None)
            self.host.tracer.log("verification_not_executed", tool=result.name, reason=result.output)
            return
        if start is None:
            raise RuntimeError("tool result has no verification ownership")
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason:
                return
            python_verification_origin = None
            operation = str(result.tool_input.get("operation") or "").strip().lower()
            if (result.name == "process" and result.executed and not result.dispatch_failed
                    and not result.permission_denied and is_host_verification_tool("process")):
                if operation == "start":
                    # 真实终态可能早于 start 展示；归属不能被 execution_settled 的早退丢弃。
                    receipt = next((receipt for receipt in result.execution_facts
                        if isinstance(receipt, ExecutionReceipt) and receipt.producer == "process"
                        and receipt.outcome == "started"), None)
                    if receipt is not None:
                        start.process_start_receipt = receipt
                elif operation == "read":
                    process_id = str(result.tool_input.get("process_id") or "").strip()
                    python_verification_origin = next((item.process_start_receipt
                        for item in self._pending.values() if item.runtime is self
                        and item.process_start_receipt is not None
                        and item.process_start_receipt.execution_id == process_id), None)
            if self.domain.execution_settled(start.execution_id):
                return
            generation = self.state.mutation_generation
            cursor = dict(self.state.workspace_observation)
        settlement = collect_workspace_settlement(result, self.store, generation, cursor,
            python_verification_origin=python_verification_origin)
        known_identities = {}
        if not settlement.complete:
            from nz_coder.tools import collect_filesystem_mutation_paths, is_filesystem_mutation_tool
            if is_filesystem_mutation_tool(result.name):
                known_identities = mutation_path_identities(self.host.workdir,
                    collect_filesystem_mutation_paths(result.tool_input))
        operations = dict(settlement.observed_changes)
        # 工具的写入意图不能证明内容发生变化；no-op 补丁不能给旧文件重贴修改身份。
        artifacts = collect_artifact_facts(self.domain.snapshot(), self.host.workdir, operations, generation)
        started = keep_execution_open or any(receipt.outcome == "started" for receipt in result.execution_facts)
        trace_events = []
        def record_trace(event, **fields):
            trace_events.append((event, fields))
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason:
                return
            state = self.state
            if generation != state.mutation_generation:
                settlement = replace(settlement, changes=None, complete=False, version_matches=False,
                    errors={**settlement.errors, "commit": "workspace version changed during scope collection"})
            if result.executed or result.workspace_scope is not None:
                state.observe_tool(result.name, result.tool_input, result.output,
                    succeeded=not (result.command_failed or result.dispatch_failed), metadata=result.metadata,
                    workspace_changes=settlement.changes,
                    workspace_scope_incomplete=result.workspace_scope is not None and not settlement.complete,
                    verification_version_valid=settlement.version_matches)
            if settlement.changes and result.name != "bash":
                from nz_coder.tools import is_filesystem_mutation_tool
                if not is_filesystem_mutation_tool(result.name):
                    state._record_workspace_mutation(list(settlement.changes), settlement.changes)
            for path in settlement.observed_changes:
                if path not in state.changed_files:
                    state.changed_files.append(path)
            if settlement.observed_changes:
                from nz_coder.runtime.agent.task_policy import is_test_file
                state.has_diff = True
                state.tests_modified = state.tests_modified or any(
                    is_test_file(path) for path in settlement.observed_changes
                    if "__pycache__" not in Path(path).parts and ".pytest_cache" not in Path(path).parts
                )
            if result.workspace_scope is not None:
                recorded = dict(cursor.get("recorded_changes", {}))
                if not settlement.complete and state.mutation_generation != generation:
                    recorded.update({path: identity for path, identity in known_identities.items()
                                     if path in state.last_mutation_paths})
                stage = result.workspace_scope.get("stage", "tool")
                state.workspace_observation = {"reference": settlement.reference if settlement.complete else cursor.get("reference", ""),
                    "generation": state.mutation_generation, "complete": settlement.complete,
                    "stage": stage, "errors": settlement.errors}
                if recorded and not settlement.complete:
                    state.workspace_observation["recorded_changes"] = recorded
                if settlement.errors:
                    record_trace("workspace_scope_incomplete", stage=stage, errors=settlement.errors)
                elif settlement.complete and not cursor.get("reference") and not cursor.get("complete", True):
                    record_trace("workspace_scope_baseline_established", stage=stage, history_observed=False)
            after = self.version()
            self.domain.observe_workspace(WorkspaceObservation(after, paths=tuple(settlement.observed_changes),
                execution_id=start.execution_id, keep_execution_open=started))
            for fact in artifacts:
                self.domain.observe_artifact(replace(fact, generation=state.mutation_generation)
                    if fact.path in operations else fact)
            before = start.version
            if settlement.complete and settlement.version_matches and before.generation == generation:
                # 恢复扫描补记的是执行前的变化；完整当前区间无变化时，本次检查已验证新版本。
                before = after if settlement.execution_unchanged else replace(before, scope_complete=True)
            facts = bind_execution_facts(result, self.domain.scope, before, after, started_revision=start.revision,
                                         origin=start.origin, zone=start.zone)
            for index, fact in enumerate(facts):
                if not fact.capabilities and any(
                    spec.source == "user_declared" and spec.capability == "custom_assert"
                    and commands_equivalent(spec.command, fact.command)
                    for spec in self.domain.snapshot().specs
                ):
                    # 检查意图由用户精确声明；实际进程结果仍须通过完整性和版本裁决。
                    fact = replace(fact, capabilities=("custom_assert",))
                if result.name not in {"bash", "process"}:
                    self._register_receipt_check(fact)
                if fact.outcome == "failed" or (fact.exit_code is not None and fact.exit_code != 0):
                    self._register_command(fact.command.original, fact.command.cwd, failed=True)
                fact = replace(fact, execution_id=start.execution_id if len(facts) == 1 else start.execution_id + ":" + fact.execution_id)
                decisions = self.domain.observe_execution(fact)
                if any(item.status in {"failed", "blocked_environment"} for item in decisions):
                    state.verification_failures += 1
                    state.needs_broad_exploration = any(item.status == "blocked_environment" for item in decisions)
                if not fact.reliable and is_check_execution(fact, self.domain.snapshot().specs):
                    record_trace("verification_result_ignored", command=fact.command.original,
                        reason="execution cannot prove a successful check", execution_id=fact.execution_id)
                for decision in decisions:
                    if decision.status == "pending" and decision.reason == "check capability not covered":
                        continue
                    spec = next(spec for spec in self.domain.snapshot().specs if spec.id == decision.check_id)
                    record_trace("verification_result", command=spec.command.original, stage=spec.stage,
                        cwd=fact.command.cwd, scope=fact.command.cwd, status=decision.status,
                        evidence_generation=fact.after.generation, output=fact.reason, execution_id=fact.execution_id)
            if not started:
                self.domain.end_execution(start.execution_id)
            from nz_coder.runtime.tool_runtime.result_projection import _semantic_evidence_metadata

            # 标签和事实属于同一次提交，后台进程不能在两者之间推进版本。
            result.evidence_metadata = _semantic_evidence_metadata(state, result)
        for event, fields in trace_events:
            self.host.tracer.log(event, **fields)
        self.refresh_plan()
        self.domain.rebuild_from_current_acceptance()

    def _register_receipt_check(self, fact: ExecutionFact) -> None:
        for capability in fact.capabilities:
            self.domain.register_check(check_spec(fact.command.original, fact.command.cwd, capability=capability,
                required=False,
                targets=fact.targets, stage="static" if capability in {"syntax", "symbol", "type", "lint", "build"} else "targeted",
                requirement_ids=()))

    def _receive_local_terminal(self, terminal):
        start = self._pending.get(terminal.launch_id)
        if start is None:
            return True
        handle = terminal.handle
        receipt = command_receipt("process", handle.command, handle.cwd,
            outcome="cancelled" if handle.status in {"cancelled", "killed"} else "failed" if handle.status == "failed" or handle.exit_code != 0 else "succeeded",
            exit_code=handle.exit_code, output=terminal.output, output_complete=terminal.output_complete,
            pipefail=terminal.pipefail)
        return self._record_terminal(start, receipt)

    def _receive_remote_terminal(self, message):
        start = current_tool_verification()
        if start is None or start.runtime is not self:
            raise ValueError("process fact has no original run ownership")
        return self._record_terminal(start, message.receipt)

    def _record_terminal(self, start, receipt):
        from nz_coder.tool_platform.execution import ToolExecutionResult
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason or self.domain.execution_settled(start.execution_id):
                return True
            generation = self.state.mutation_generation
            before = self.state.workspace_observation.get("reference") or start.before_reference
        errors = {}
        try:
            after = self.store.verification_snapshot()
        except SnapshotError as exc:
            after = None
            errors["after"] = str(exc)
        result = ToolExecutionResult("process", {"operation": "start", "command": receipt.command}, receipt.reason, True, False,
            receipt.outcome == "failed", False, execution_facts=(receipt,), execution_id=start.execution_id,
            workspace_scope={"workspace": str(self.host.workdir), "generation": generation, "before": before, "after": after,
                             "stage": "terminal", "errors": errors})
        self.record_result(result)
        return True

    def refresh_plan(self) -> None:
        from nz_coder.intelligence.verification_planner import plan_verification_commands
        from nz_coder.runtime.core.execution_context import strict_local_tools
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason:
                return
            signature = (self.state.source_mutation_generation, tuple(self.state.changed_files))
            if self.state.acceptance_mutation_generation == 0 or signature == self._plan_signature:
                return
            candidates = tuple(self.state.changed_files)
        try:
            paths = tuple(path for path in candidates if (self.host.workdir / path).is_file()
                          and (self.host.workdir / path).resolve().is_relative_to(self.host.workdir))
            from nz_coder.runtime.process.workdir import scoped_workdir
            with scoped_workdir(self.host.workdir):
                plan = plan_verification_commands(changed_files=list(paths), include_broad=False,
                                                  require_targeted=strict_local_tools())
        except OSError as error:
            self.host.tracer.log("verification_plan_unavailable", error_type=type(error).__name__)
            return
        plan["changed_files"] = list(paths)
        with self.run_context._state_lock:
            if self.domain.snapshot().terminal_reason or signature != (self.state.source_mutation_generation, tuple(self.state.changed_files)):
                return
            register_verification_plan(self.domain, plan)
            self._native_runner_kind = str(plan.get("native_runner_kind") or "")
            self._plan_signature = signature
            self.synchronize_task()

    def prepare_freeze(self) -> int | None:
        """先交接已退出命令，再在锁外采集最终工作区；不把运行中命令当作完成。"""
        import time
        from nz_coder.runtime.process.command_channel import current_command_client
        client = current_command_client()
        deadline = min(client.deadline, time.monotonic() + 5.0) if client is not None else time.monotonic() + 5.0
        watermark = None
        delivered = True
        try:
            if client is not None:
                watermark = client.facts_barrier(deadline=deadline)
            elif self._local_service is not None:
                delivered = self._local_service.wait_terminal_delivery(deadline=deadline)
        except (RuntimeError, TimeoutError):
            delivered = False
        self._reconcile_workspace(delivered=delivered)
        return watermark

    def reconcile_workspace(self) -> None:
        """事务恢复后独立核对真实磁盘，不继承已取消工具的事件或封存通道。"""
        self._reconcile_workspace(delivered=True, stage="reconcile")
        self.refresh_plan()

    def _reconcile_workspace(self, *, delivered: bool, stage: str = "freeze") -> None:
        with self.run_context._state_lock:
            before = self.version()
            cursor = dict(self.state.workspace_observation)
            snapshot = self.domain.snapshot()
        complete, reference, changes = False, "", {}
        errors = {}
        try:
            reference = self.store.verification_snapshot()
            if cursor.get("reference"):
                changes = self.store.verification_changes(cursor["reference"], reference)
            # 缺少旧基线时只建立当前基线，不能从现有文件倒推未知期间的修改。
            complete = True
        except SnapshotError as exc:
            errors["capture"] = str(exc)
        unaccounted = unaccounted_workspace_changes(self.store, changes, cursor)
        advances = bool(unaccounted)
        artifacts = collect_artifact_facts(snapshot, self.host.workdir, changes, before.generation + int(advances))
        with self.run_context._state_lock:
            if self.version() != before:
                complete = False
                errors["commit"] = "workspace version changed during scope collection"
            elif advances:
                self.state._record_workspace_mutation(list(unaccounted), unaccounted)
            for path in changes:
                if path not in self.state.changed_files:
                    self.state.changed_files.append(path)
            self.state.workspace_observation = {"reference": reference if complete else cursor.get("reference", ""),
                "generation": self.state.mutation_generation, "complete": complete and delivered,
                "stage": stage, "errors": errors}
            if not complete and cursor.get("recorded_changes"):
                self.state.workspace_observation["recorded_changes"] = cursor["recorded_changes"]
            if not delivered:
                errors["delivery"] = "process terminal facts could not be delivered"
            self.domain.observe_workspace(WorkspaceObservation(self.version(), paths=tuple(changes)))
            if complete:
                for fact in artifacts:
                    self.domain.observe_artifact(fact)
        if errors:
            self.host.tracer.log("workspace_scope_incomplete", stage=stage, errors=errors)
        elif complete and not cursor.get("reference") and not cursor.get("complete", True):
            self.host.tracer.log("workspace_scope_baseline_established", stage=stage, history_observed=False)
        self.domain.rebuild_from_current_acceptance()

    def finish_freeze(self, watermark: int | None) -> None:
        if watermark is not None:
            from nz_coder.runtime.process.command_channel import current_command_client
            current_command_client().facts_sealed(watermark)

    def status(self) -> dict:
        return self.domain.snapshot().to_dict()

    def should_gate(self) -> bool:
        return not self.domain.snapshot().execution_ready

    def increment_gate_prompt(self) -> int:
        self._gate_prompts += 1
        return self._gate_prompts

    @property
    def gate_prompts(self) -> int:
        return self._gate_prompts

    def make_gate_message(self) -> str:
        snapshot = self.domain.snapshot()
        status = snapshot.to_dict()
        required = [item for stage in status["verification_pipeline"]["stages"] for item in stage["commands"]
                    if item["required"] and item["status"] not in {"passed", "covered"}]
        obligations = [item for item in status["test_obligations"] if item["status"] != "passed"]
        required = list({(item["command"], item["cwd"]): item for item in [*required, *obligations]}.values())
        lines = ["Verification remains unconfirmed. Run the original checks against the current workspace."]
        if snapshot.targeted_pending:
            lines.append("Run a direct narrow behavioral test with a non-empty selection.")
        if obligations:
            lines.append("Failed checks that must be rerun after fixing:")
        lines.extend(item["retry_command"] for item in required[:6])
        related = [spec.command.original for spec in snapshot.specs
                   if not spec.required and spec.stage == "targeted" and snapshot.targeted_pending
                   and snapshot.retry_eligible(spec)]
        if related:
            lines.append("Recommended high-confidence related checks:")
            lines.extend(related[:4])
        return "\n".join(lines)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
