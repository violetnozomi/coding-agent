"""域测试的事实夹具；通过真实域/适配器计算结论，没有第二份台账。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
from nz_coder.runtime.adapters.verification_facts import command_receipt, bind_execution_facts
from nz_coder.runtime.verification.domain.models import ArtifactFact, ReviewFact, RunScope, TaskScope, WorkspaceObservation, WorkspaceVersion
from nz_coder.runtime.verification.domain.requirements import TaskContract, RequirementLedgerView
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.agent.task_policy import is_documentation_file, test_command_targets
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore, SnapshotError


class VerificationScenario:
    def __init__(self, contract: TaskContract | None = None, workspace=None, plan=None, require_targeted=False):
        self._temporary = TemporaryDirectory(prefix="nz-domain-fixture-") if workspace is None else None
        self.root = Path(self._temporary.name if self._temporary is not None else workspace).resolve()
        request = RunRequest(agent=AgentDefinition(name="test", instructions="test"), profile=MAIN_PROFILE,
                             workspace=self.root, session_id="test", messages=())
        self.context = RunContext(request, Session.create("test", [], workspace=self.root), "test")
        scope = RunScope(TaskScope("test", "task", str(self.root)), self.context.interaction_run_id, "test")
        self.domain = VerificationDomain(lock=self.context._state_lock, scope=scope, version=WorkspaceVersion(), require_targeted=require_targeted)
        self.context.attach_verification(self.domain)
        self.state = RuntimeState()
        self.state.bind_run_context(self.context)
        self.state.set_task_contract(contract or TaskContract())
        self.store = WorkspaceSnapshotStore(self.root, self.root / ".nz-coder" / "test-snapshots")
        try:
            reference = self.store.verification_snapshot()
        except SnapshotError:
            reference = ""
        self.state.workspace_observation = {"reference": reference, "generation": 0, "complete": bool(reference)}
        self.runtime = VerificationRuntime(SimpleNamespace(runtime_state=self.state, workspace_snapshots=self.store,
            tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
            workdir=self.root, _best_effort_tool_input=lambda value: __import__("json").loads(value) if isinstance(value, str) else value), self.context)
        self._unit_plan = plan
        if plan is not None:
            from nz_coder.runtime.adapters.verification_plans import register_verification_plan
            register_verification_plan(self.domain, plan)
            self.runtime.refresh_plan = lambda: None

    @property
    def ledger(self):
        return RequirementLedgerView.from_snapshot(self.domain.snapshot())

    @property
    def items(self):
        return self.ledger.items

    @property
    def latest_generation(self):
        return self.ledger.latest_generation

    def status(self, id):
        return self.ledger.status(id)

    def unresolved(self):
        return self.ledger.unresolved()

    def to_dict(self):
        return self.ledger.to_dict()

    def semantic_review_pending_only(self, **kwargs):
        return self.ledger.semantic_review_pending_only(**kwargs)

    def semantic_review_ready(self, *args, **kwargs):
        return self.ledger.semantic_review_ready(*args, **kwargs)

    def mutate(self, generation, paths, operations=None):
        old = self.domain.snapshot().version
        relevant = not paths or not all(is_documentation_file(path) for path in paths)
        version = WorkspaceVersion(generation, generation if relevant else old.source_generation,
                                   generation if relevant else old.acceptance_generation,
                                   old.scope_complete and bool(paths))
        with self.context._state_lock:
            self.state.mutation_generation = version.generation
            self.state.source_mutation_generation = version.source_generation
            self.state.acceptance_mutation_generation = version.acceptance_generation
            for path in paths:
                if path not in self.state.changed_files:
                    self.state.changed_files.append(path)
            self.state.has_diff = self.state.has_diff or bool(paths)
            self.domain.observe_workspace(WorkspaceObservation(version, paths=tuple(paths)))
            for path in paths:
                operation = (operations or {}).get(path, "change")
                import hashlib
                target = self.root / path
                fingerprint = f"fixture:{path}:{generation}"
                try:
                    if target.resolve().is_relative_to(self.root) and target.is_file():
                        fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
                    elif operation == "delete":
                        fingerprint = hashlib.sha256((path + ":deleted").encode()).hexdigest()
                except OSError:
                    pass
                self.domain.observe_artifact(ArtifactFact(self.domain.scope.task, path, operation,
                    generation, fingerprint, present=operation != "delete"))

    def execute(self, generation, *, command, passed, acceptance):
        if acceptance:
            self.state.verification_definition = {"command": command, "targets": list(test_command_targets(command))}
            # 完整宿主合同才是验收来源，单条展示投影不能再创建权限。
            current = self.domain.task_definition.contract or TaskContract()
            self.state.set_task_contract(replace(current, acceptance_commands=(command,)))
            self.runtime.synchronize_task()
        self.runtime._register_command(command, str(self.root), failed=not passed)
        before = self.domain.snapshot().version
        if generation > before.generation:
            # 夹具显式给定命令开始时的版本；不从旧验证结果推导新版本。
            before = WorkspaceVersion(generation, generation, generation, before.scope_complete)
            self.domain.observe_workspace(WorkspaceObservation(before))
            self.state.mutation_generation = generation
            self.state.source_mutation_generation = generation
            self.state.acceptance_mutation_generation = generation
        elif generation < before.generation:
            before = replace(before, acceptance_generation=generation)
        receipt = command_receipt("bash", command, str(self.root),
                                  outcome="succeeded" if passed else "failed", exit_code=0 if passed else 1,
                                  output="1 passed" if passed else "1 failed")
        result = ToolExecutionResult("bash", {"command": command}, receipt.reason, True, False, not passed, False,
                                     execution_facts=(receipt,))
        for fact in bind_execution_facts(result, self.domain.scope, before, self.domain.snapshot().version):
            self.domain.begin_execution(fact.execution_id, ())
            self.domain.observe_execution(fact)
        return self.ledger

    def review(self, generation, *, accepted, fingerprint="fixture-review", acceptance_required=True):
        snapshot = self.domain.snapshot()
        self.domain.observe_review(ReviewFact(uuid4().hex, self.domain.scope.task,
            tuple(item.requirement.id for item in snapshot.requirements), snapshot.version,
            fingerprint, accepted, basis_fingerprint=self.domain.review_basis() if generation == snapshot.version.generation else "old materials"))

    def refresh_artifacts(self, workspace):
        from nz_coder.runtime.adapters.verification_workspace import collect_artifact_facts
        snapshot = self.domain.snapshot()
        for fact in collect_artifact_facts(snapshot, Path(workspace), {}, snapshot.version.generation):
            self.domain.observe_artifact(fact)

    def settle_shell(self, command, cwd=None):
        from nz_coder.runtime.process.workdir import scoped_workdir
        from nz_coder.runtime.process.workspace_scope_execution import execute_observed
        from nz_coder.tools.bash import run_bash
        args = {"command": command, "workdir": str(cwd or self.root)}
        call = {"id": uuid4().hex, "function": {"name": "bash", "arguments": args}}
        with scoped_workdir(self.root):
            start = self.runtime.begin_tool(call)
            def execute():
                raw = run_bash(command, workdir=args["workdir"], timeout=3)
                assert hasattr(raw, "execution_facts"), str(raw)
                failed = any(item.outcome == "failed" for item in raw.execution_facts)
                return ToolExecutionResult("bash", args, str(raw), True, False, failed, False,
                    execution_facts=raw.execution_facts, metadata=raw.metadata)
            result = execute_observed(self.store, start.version.generation, execute,
                on_before=lambda reference: self.runtime.record_before(start, reference))
            result.execution_id = start.execution_id
            self.runtime.record_result(result)
            return result

    def settle_diff(self, output):
        call = {"id": uuid4().hex, "function": {"name": "diff_status", "arguments": {}}}
        start = self.runtime.begin_tool(call)
        result = ToolExecutionResult("diff_status", {}, output, True, False, False, False, execution_id=start.execution_id)
        self.runtime.record_result(result)

    def write_resource(self, name, payload, *, output="", dispatch_failed=False):
        if dispatch_failed:
            return
        before = self.state.mutation_generation
        self.state.observe_tool(name, payload, output, succeeded=True)
        if self.state.mutation_generation != before:
            from nz_coder.runtime.agent.task_policy import successful_mutation_operations
            paths = self.state.last_mutation_paths
            self.mutate(self.state.mutation_generation, paths,
                operations=successful_mutation_operations(name, payload, output))
            if self._unit_plan is not None:
                from nz_coder.runtime.adapters.verification_plans import register_verification_plan
                builder = getattr(self, "_unit_plan_builder", None)
                selected = builder(list(self.state.changed_files)) if builder is not None else self._unit_plan
                self.runtime._native_runner_kind = str(selected.get("native_runner_kind") or "")
                plan = {**selected, "changed_files": list(self.state.changed_files)}
                register_verification_plan(self.domain, plan)

    def command_result(self, tool_input, output, dispatch_failed, command_failed, *, exit_code,
                       mutation_in_same_call=False):
        self.runtime.synchronize_task()
        command = str(tool_input.get("command") or "")
        cwd = str((self.root / str(tool_input.get("workdir") or ".")).resolve())
        ids = self.runtime._register_command(command, cwd, failed=command_failed or (exit_code is not None and exit_code != 0))
        execution_id = uuid4().hex
        self.domain.begin_execution(execution_id, ids)
        if dispatch_failed:
            self.domain.end_execution(execution_id)
            return
        before = self.domain.snapshot().version
        if mutation_in_same_call:
            self.mutate(before.generation + 1, [])
        body = str(output)
        prefix = f"Command exited with code {exit_code}"
        if body.startswith(prefix):
            body = body[len(prefix):].lstrip("\r\n")
        from nz_coder.runtime.verification.evidence import is_environment_verification_failure
        env_error = is_environment_verification_failure(command, str(output), self.state.changed_files,
            strict_offline=self.domain._require_targeted, exit_code=exit_code)
        receipt = command_receipt("bash", command, cwd, outcome="failed" if command_failed else "succeeded",
            exit_code=exit_code, output=body, environment_error=env_error)
        result = ToolExecutionResult("bash", tool_input, str(output), True, False, command_failed, False,
            execution_facts=(receipt,))
        for fact in bind_execution_facts(result, self.domain.scope, before, self.domain.snapshot().version):
            self.domain.observe_execution(replace(fact, execution_id=execution_id))

    def syntax_result(self, output, *, passed):
        # 单元夹具明确声明“已执行语法检查”，不把聚合文字扩成任意检查能力。
        for path in self.state.changed_files:
            if Path(path).suffix == ".py":
                self.command_result({"command": "python -m py_compile " + __import__("shlex").quote(path)},
                    output, False, not passed, exit_code=0 if passed else 1)

    def symbol_result(self, output, tool_input=None, *, passed):
        import shlex
        from nz_coder.runtime.verification.domain.models import ExecutionReceipt
        path = str((tool_input or {}).get("path") or self.state.last_mutation_paths[0])
        command = shlex.join(["python_symbol_check", path])
        receipt = ExecutionReceipt("python_symbol_check", command, str(self.root),
            "succeeded" if passed else "failed", capabilities=("syntax", "symbol"),
            targets=(path,), in_process=True, reason=output)
        result = ToolExecutionResult("python_symbol_check", {"path": path}, output, True, False, not passed, False,
            execution_facts=(receipt,))
        for fact in bind_execution_facts(result, self.domain.scope, self.domain.snapshot().version, self.domain.snapshot().version):
            self.runtime._register_receipt_check(fact)
            self.domain.begin_execution(fact.execution_id, ())
            self.domain.observe_execution(fact)

    def acceptance_result(self, command, output, *, passed, workdir=None):
        self.state.verification_definition = {"command": command, "targets": list(test_command_targets(command))}
        self.runtime.synchronize_task()
        if command:
            self.command_result({"command": command, "workdir": str(workdir or self.root)}, output,
                                False, not passed, exit_code=0 if passed else 1)

    def restart(self):
        temporary = self._temporary
        self.__init__(workspace=self.root, plan=self._unit_plan, require_targeted=self.domain._require_targeted)
        self._temporary = temporary

    def restore(self, checkpoint, *, workspace, generation):
        from nz_coder.runtime.verification.domain.checkpoint import restore_domain
        from nz_coder.runtime.core.result import RunStatus
        self.context.finish(RunStatus.INTERRUPTED)
        request = replace(self.context.request, workspace=Path(workspace), interaction_run_id=None)
        self.context = RunContext(request, Session.create("test", [], workspace=workspace), "test")
        scope = RunScope(TaskScope("test", "task", str(Path(workspace).resolve())), self.context.interaction_run_id, "test")
        version = WorkspaceVersion(generation, generation, generation,
            bool(checkpoint.get("version", {}).get("scope_complete", True)))
        self.domain = restore_domain(checkpoint, lock=self.context._state_lock, scope=scope, version=version)
        self.context.attach_verification(self.domain)
        self.state = RuntimeState()
        self.state.bind_run_context(self.context)
        self.state.mutation_generation = generation
        self.state.source_mutation_generation = generation
        self.state.acceptance_mutation_generation = generation
        self.state.task_authority_epoch = self.domain.task_definition.authority_revision
        self.state.task_authority_message_id = self.domain.task_definition.authority_message_id
        self.state.set_task_contract(self.domain.task_definition.contract or TaskContract())
        self.root = Path(workspace).resolve()
        self.runtime = VerificationRuntime(SimpleNamespace(runtime_state=self.state, workspace_snapshots=self.store,
            workdir=self.root, _best_effort_tool_input=lambda value: value), self.context)
        self.runtime.refresh_plan = lambda: None
