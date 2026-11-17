"""当前工作路线的提交与展示，以及可选动作交接。"""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess, WorkspaceFileIdentity
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, PARTS_KEY, SESSION_ID_KEY, COMPACTION_KEY
from nz_coder.runtime.verification.domain.models import TaskScope


@dataclass(frozen=True)
class WorkingPlanScope:
    """请求前固定的计划来源；正文不拥有授权或验收状态。"""

    task: TaskScope
    interaction_run_id: str
    agent_id: str
    instruction_message_id: str
    authority_epoch: int
    revision: int
    assistant_message_id: str


def parse_working_plan_update(payload: dict) -> str:
    if not isinstance(payload, dict) or set(payload) != {"plan", "expected_revision"}:
        raise ValueError("working plan requires only plan and expected_revision")
    if type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    steps = payload["plan"]
    if not isinstance(steps, list) or any(
        not isinstance(step, dict) or set(step) - {"title", "target", "verification"}
        for step in steps
    ):
        raise ValueError("working plan steps allow only title, target and verification")
    return parse_working_plan(json.dumps({"plan": steps}, ensure_ascii=False))


def _plan_instruction(messages) -> str:
    from nz_coder.protocol.message_schema import is_user_request_message
    source = next((message for message in reversed(messages) if is_user_request_message(message)), None)
    return str(source.get(MESSAGE_ID_KEY) or "") if source is not None else ""


def capture_working_plan_scope(state, run_context, assistant_message_id: str, messages) -> WorkingPlanScope | None:
    if run_context is None or run_context.verification is None:
        return None
    with run_context._state_lock:
        instruction = _plan_instruction(messages)
        if not instruction:
            return None
        return WorkingPlanScope(run_context.verification.scope.task, run_context.interaction_run_id,
            run_context.verification.scope.agent_id, instruction, state.task_authority_epoch,
            state.plan_revision, assistant_message_id)


def _check_plan_source(state, scope, run, superseded) -> None:
    if not isinstance(scope, WorkingPlanScope) or run is None or state._verification_context is not run:
        raise ValueError("working plan request context is unavailable")
    if (run.cancel_scope.is_set() or run.finalized or run.terminal_status is not None or superseded()
            or scope.task != run.verification.scope.task
            or scope.agent_id != run.verification.scope.agent_id
            or scope.interaction_run_id != run.interaction_run_id
            or scope.authority_epoch != state.task_authority_epoch
            or scope.instruction_message_id != _plan_instruction(run.transcript)):
        raise ValueError("working plan request is cancelled, frozen or superseded")


def _commit_working_plan(state, scope, run, text, payload, call_id, *, source_kind, path, superseded) -> dict:
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
    from nz_coder.runtime.conversation.runtime_checkpoint import build_runtime_checkpoint

    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with state._persistence_lock:
        with run._state_lock:
            _check_plan_source(state, scope, run, superseded)
            previous = state.plan_last_commit
            if (previous and previous.get("source_kind") == source_kind
                    and previous.get("scope") == asdict(scope) and previous.get("call_id") == call_id):
                if previous["payload_hash"] != fingerprint:
                    raise ValueError("same working plan call has a different payload")
                return {"status": "already_accepted", "revision": previous["revision"],
                        "persisted": previous["persisted"], "reason": ""}
            if payload["expected_revision"] != scope.revision or scope.revision != state.plan_revision:
                raise ValueError(f"stale working plan revision; expected_revision={state.plan_revision}")
            status = "unchanged" if text == state.plan_text else "accepted"
            receipt = {"source_kind": source_kind, "scope": asdict(scope), "call_id": call_id,
                       "payload_hash": fingerprint, "revision": state.plan_revision + 1,
                       "status": status, "persisted": path is not None}
            candidate = build_runtime_checkpoint(state, active=True)
            candidate.update(plan_text=text, plan_revision=receipt["revision"], plan_last_commit=receipt,
                             plan_generated=True if source_kind == "initial" else state.plan_generated)
            before = (state.plan_revision, state.plan_text, state.plan_last_commit)
        if path is not None:
            state._write_snapshot(path, candidate)
        failure = None
        with run._state_lock:
            try:
                _check_plan_source(state, scope, run, superseded)
                if before != (state.plan_revision, state.plan_text, state.plan_last_commit):
                    raise ValueError("working plan changed during save")
            except ValueError as exc:
                failure = exc
                rollback = build_runtime_checkpoint(state, active=True)
            else:
                state.plan_text = text
                state.plan_revision = receipt["revision"]
                state.plan_last_commit = receipt
                state.plan_generated = candidate["plan_generated"]
        if failure is not None:
            if path is not None:
                try:
                    state._write_snapshot(path, rollback)
                except OSError as exc:
                    raise PublicRuntimeError(PublicError("working_plan_rollback_failed",
                        "Working plan checkpoint could not be restored.")) from exc
            raise failure
    return {"status": status, "revision": receipt["revision"], "persisted": path is not None, "reason": ""}


def submit_working_plan(state, scope, call, result, messages, *, run_context, path=None, superseded=lambda: False):
    from nz_coder.protocol.public_error import to_public_error

    proposal = (result.metadata or {}).get("working_plan_proposal")
    if proposal is None or result.dispatch_failed:
        return result
    try:
        if not isinstance(scope, WorkingPlanScope) or run_context is None or not scope.assistant_message_id:
            raise ValueError("working plan request context is unavailable")
        if result.name != "update_working_plan" or not result.executed or call.get("function", {}).get("name") != result.name:
            raise ValueError("only executed update_working_plan may publish a route")
        raw = call["function"].get("arguments", {})
        args = json.loads(raw) if isinstance(raw, str) else raw
        text = parse_working_plan_update(args)
        if args != proposal or args != result.tool_input:
            raise ValueError("working plan proposal differs from the actual tool input")
        owners = [message for message in messages if message.get(MESSAGE_ID_KEY) == scope.assistant_message_id
                  and message.get("role") == "assistant" and any(item == call for item in message.get("tool_calls", []))]
        if len(owners) != 1 or not call.get("id"):
            raise ValueError("working plan source call is unavailable or ambiguous")
        receipt = _commit_working_plan(state, scope, run_context, text, args, call["id"],
            source_kind="tool", path=path, superseded=superseded)
    except (ValueError, TypeError, OSError) as exc:
        result.dispatch_failed = True
        reason = to_public_error(exc).message if isinstance(exc, OSError) else str(exc)
        receipt = {"status": "rejected", "revision": state.plan_revision, "persisted": False, "reason": reason}
        result.output = "Error: working plan rejected: " + reason
    else:
        result.output = (f"Working plan {receipt['status']}; expected_revision={receipt['revision']}; "
                         f"persisted={str(receipt['persisted']).lower()}.")
    result.metadata = {**dict(result.metadata or {}), "working_plan_commit": receipt}
    return result


def install_initial_working_plan(state, scope, plan_text, *, run_context, path=None, superseded=lambda: False):
    if not isinstance(scope, WorkingPlanScope):
        raise ValueError("initial working plan request context is unavailable")
    return _commit_working_plan(state, scope, run_context, plan_text,
        {"plan_text": plan_text, "expected_revision": scope.revision},
        "initial-plan:" + scope.interaction_run_id, source_kind="initial", path=path, superseded=superseded)


def render_working_plan_revision(state) -> str:
    return ("<working-plan-version>\nAdvisory route only; update_working_plan "
            f"expected_revision={state.plan_revision}. Not execution or verification evidence.\n"
            "</working-plan-version>")


def sanitize_working_plan_receipt(state) -> None:
    """恢复正文仍是建议；不完整的回执不能获得重放确认权。"""
    record = state.plan_last_commit
    try:
        scope = record["scope"]
        task = TaskScope(**scope["task"])
        source = WorkingPlanScope(task=task, **{key: value for key, value in scope.items() if key != "task"})
        valid = (record["source_kind"] in {"initial", "tool"}
                 and type(record["revision"]) is int and record["revision"] == state.plan_revision
                 and type(source.revision) is int and source.revision >= 0
                 and source.revision + 1 == state.plan_revision
                 and record["status"] in {"accepted", "unchanged"}
                 and type(record["persisted"]) is bool
                 and isinstance(record["call_id"], str) and bool(record["call_id"])
                 and isinstance(record["payload_hash"], str) and len(record["payload_hash"]) == 64
                 and all(isinstance(value, str) and value for value in (
                     source.interaction_run_id, source.agent_id, source.instruction_message_id))
                 and type(source.authority_epoch) is int and source.authority_epoch >= 0
                 and isinstance(source.assistant_message_id, str)
                 and (bool(source.assistant_message_id) if record["source_kind"] == "tool" else source.assistant_message_id == ""))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        state.plan_last_commit = {}


def invalidate_working_plan(state=None, *, path=None) -> None:
    """空闲转换先使建议失效；磁盘其他执行事实不得被未加载的内存覆盖。"""
    from contextlib import nullcontext
    from nz_coder.runtime.conversation.runtime_checkpoint import build_runtime_checkpoint

    # HTTP 的已恢复/无运行宿主会话可能只有检查点，无内存正文可清理。
    run = state._verification_context if state is not None else None
    with state._persistence_lock if state is not None else nullcontext():
        with run._state_lock if run is not None else nullcontext():
            if run is not None and not run.finalized:
                raise ValueError("cannot invalidate working plan during an active run")
            empty = {"plan_text": "", "plan_generated": False, "plan_revision": 0, "plan_last_commit": {}}
            if path is not None:
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    if state is None:
                        return
                    snapshot = build_runtime_checkpoint(state, active=False)
                if not isinstance(snapshot, dict):
                    raise ValueError("invalid working plan checkpoint")
                snapshot.update(empty)
                from nz_coder.runtime.conversation.runtime_checkpoint import write_runtime_snapshot
                writer = state._write_snapshot if state is not None else write_runtime_snapshot
                writer(path, snapshot)
            if state is not None:
                for key, value in empty.items():
                    setattr(state, key, value)


def parse_working_plan(raw_text: str) -> str:
    """只保留阶段路线字段；模型额外生成的合同不参与解析或验收。"""
    try:
        payload = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("working plan must be a JSON object") from exc
    steps = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("working plan requires nonempty plan steps")
    lines = ["## Plan"]
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not isinstance(step.get("title"), str) or not step["title"].strip():
            raise ValueError("working plan step requires a title")
        target = step.get("target", "need to search")
        verification = step.get("verification", "focused verification")
        if not isinstance(target, str) or not isinstance(verification, str):
            raise ValueError("working plan target and verification must be text")
        lines.append(f"{index}. {step['title'].strip()} - {target.strip()} - {verification.strip()}")
    return "\n".join(lines)


def planning_task_context(state) -> dict:
    """使用当前合同定义与真实用户原文，不把旧摘要当完整授权。"""
    from copy import deepcopy
    from nz_coder.protocol.message_schema import is_user_request_message

    # processor 在重规划前已解绑；整轮输入只能从已绑定 RunContext 获取。
    context = state._verification_context
    messages = context.transcript if context is not None else []
    source = next((message for message in reversed(messages) if is_user_request_message(message)), None)
    content = source.get("content", "") if source is not None else state.current_round_instruction_text
    if isinstance(content, str):
        current = content
    else:
        # 先选当前来源再取文本，纯图片输入不能让更早的用户消息冒充当前要求。
        current = "\n".join(part["text"] for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                            and isinstance(part.get("text"), str)) if isinstance(content, list) else ""
    contract = state.task_contract
    return {"original_task": state.initial_task_text, "current_instruction": current,
            "requirements": deepcopy(contract.get("requirements", [])),
            "constraints": list(contract.get("constraints", [])),
            "acceptance_commands": list(contract.get("acceptance_commands", []))}


@dataclass(frozen=True)
class ExecutionHandoff:
    """唯一当前动作；performed 仅表示动作发生，不表示需求满足。"""

    hypothesis: str
    kind: str
    evidence: list[str]
    targets: list[dict]
    action: str
    verification_command: str
    verification_cwd: str
    epoch: int
    task: str
    source_message: str
    source_call: str
    source_session: str
    workspace: str
    generation: int
    verification_failures: int
    status: str = "pending"
    source_confirmed: bool = True


def _text(value, name: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"invalid {name} (maximum {limit} characters)")
    return value


def _task_key(state) -> str:
    return hashlib.sha256(state.initial_task_text.encode("utf-8")).hexdigest()


def _call_owner(messages: list, call_id: str) -> tuple[int, dict]:
    owners = [(index, message) for index, message in enumerate(messages)
              if message.get("role") == "assistant"
              for call in message.get("tool_calls", []) if call.get("id") == call_id]
    if len(owners) != 1 or not owners[0][1].get(MESSAGE_ID_KEY):
        raise ValueError("source call/message identity is unavailable or ambiguous")
    return owners[0]


def _seen_evidence(messages: list) -> dict[str, dict]:
    """只使用提交消息之前、已有落定输出的调用，不建立历史索引。"""
    outputs = {m.get("tool_call_id"): m for m in messages if m.get("role") == "tool"}
    evidence = {}
    for message in messages:
        for part in message.get(PARTS_KEY, []):
            call_id = part.get("call_id")
            state = part.get("state") or {}
            if part.get("type") != "tool" or call_id not in outputs or state.get("status") not in {"completed", "error"}:
                continue
            evidence[call_id] = part
            metadata = state.get("metadata") or {}
            artifact = metadata.get("raw_artifact_id")
            if isinstance(artifact, str) and artifact in str(outputs[call_id].get("content", "")):
                evidence[artifact] = part
    return evidence


def _identity(access: WorkspaceFileAccess, path: str) -> dict:
    try:
        _, identity = access.read_bytes_with_identity(path)
    except FileNotFoundError:
        identity = WorkspaceFileIdentity.missing()
    return asdict(identity)


def _display_handoff(value: dict) -> str:
    lines = ["<execution-handoff>", f"Next action ({value['status']}): {value['kind']}",
             "Hypothesis (not verified): " + value["hypothesis"], "Action: " + value["action"]]
    lines.extend("Target: " + target["path"] + " | symbol=" + target["symbol"]
                 + " | " + target["operation"] for target in value["targets"])
    if value["evidence"]:
        lines.append("Evidence: " + ", ".join(value["evidence"]))
    if value["verification_command"]:
        lines.extend(["Verification command: " + value["verification_command"],
                      "Verification cwd: " + value["verification_cwd"]])
    lines.extend(["Execution is not acceptance; use current verification evidence.", "</execution-handoff>"])
    return "\n".join(lines)


def _prepare_handoff(state, proposal: dict, messages: list, call_id: str, workspace) -> dict:
    from nz_coder.tools import is_filesystem_mutation_tool
    source_index, source = _call_owner(messages, call_id)
    if any(is_filesystem_mutation_tool(call.get("function", {}).get("name", ""))
           for call in source.get("tool_calls", [])):
        raise ValueError("submit the handoff in a separate batch before editing")
    action = proposal.get("next_action")
    if not isinstance(action, dict) or set(action) - {
        "kind", "evidence", "targets", "action", "verification_command", "verification_cwd",
    }:
        raise ValueError("invalid next_action fields")
    kind = action.get("kind")
    if kind not in {"inspect", "edit", "verify", "blocked"}:
        raise ValueError("kind must be inspect, edit, verify or blocked")
    refs = action.get("evidence", [])
    if not isinstance(refs, list) or len(refs) > 5:
        raise ValueError("evidence must contain at most 5 references")
    seen = _seen_evidence(messages[:source_index])
    for ref in refs:
        _text(ref, "evidence ID", 200)
        if ref not in seen:
            raise ValueError("evidence is not a visible settled tool/artifact: " + ref)
    targets = action.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 3 or (kind == "edit" and not targets):
        raise ValueError("targets must contain at most 3 paths; edit requires a target")
    access = WorkspaceFileAccess(workspace)
    bound = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"path", "symbol", "operation"}:
            raise ValueError("target requires path, symbol, operation")
        raw_path = _text(target["path"], "target path", 240)
        path = str(access.display_path(raw_path, write=kind == "edit").relative_to(access.root))
        symbol = _text(target["symbol"], "symbol", 120, required=False)
        operation = target["operation"]
        if operation not in {"modify", "create", "read"}:
            raise ValueError("operation must be modify, create or read")
        current = _identity(access, path)
        observed = None
        if operation == "create":
            if current["expected_exists"]:
                raise ValueError("new target already exists: " + path)
            observed = current
        else:
            for ref in refs:
                part = seen[ref]
                observation = (part.get("state", {}).get("metadata") or {}).get("model_read_observation") or {}
                if observation.get("path") and str(access.display_path(observation["path"]).relative_to(access.root)) == path:
                    observed = observation.get("identity")
            if observed is None and kind == "inspect":
                observed = current
            if observed is None or observed != current:
                raise ValueError("target must be re-read; observed version is missing or stale: " + path)
        bound.append({"path": path, "symbol": symbol, "operation": operation, "identity": observed})
    command = _text(action.get("verification_command", ""), "verification command", 500, required=kind == "verify")
    cwd = _text(action.get("verification_cwd", "."), "verification cwd", 240)
    access.display_path(cwd)
    value = asdict(ExecutionHandoff(
        hypothesis=_text(proposal.get("hypothesis"), "hypothesis", 500), kind=kind, evidence=refs,
        targets=bound, action=_text(action.get("action"), "action", 500),
        verification_command=command, verification_cwd=cwd,
        epoch=state.task_authority_epoch, task=_task_key(state),
        source_message=source[MESSAGE_ID_KEY], source_call=call_id,
        source_session=str(source.get(SESSION_ID_KEY) or ""), workspace=str(access.root),
        generation=state.mutation_generation,
        verification_failures=state.verification_failures,
    ))
    if len(_display_handoff(value)) > 1500:
        raise ValueError("handoff display exceeds 1500 characters; shorten complete fields")
    return value


def submit_execution_handoff(state, call, result, messages, *, workspace, path=None):
    """在工具结果投影之前接受；错误回执不得误报已保存。"""
    proposal = (result.metadata or {}).get("execution_handoff_proposal")
    if proposal is None or result.dispatch_failed:
        return result
    try:
        if (result.name != "update_scratchpad" or not result.executed
                or call.get("function", {}).get("name") != "update_scratchpad"):
            raise ValueError("only executed update_scratchpad may submit an execution handoff")
        if not isinstance(proposal, dict):
            raise ValueError("invalid execution handoff proposal")
        _, source = _call_owner(messages, str(call.get("id") or ""))
        previous = state.execution_handoff
        duplicate = (previous.get("source_call") == call.get("id")
                     and previous.get("source_message") == source[MESSAGE_ID_KEY])
        if not duplicate:
            value = _prepare_handoff(state, proposal, messages, call["id"], workspace)
            state.commit_execution_handoff(value, path)
        receipt = "Execution handoff accepted" + (" (current process only)" if path is None else " (saved)")
        return replace(result, output=receipt, metadata={"execution_handoff_accepted": state.execution_handoff["source_call"]})
    except (ValueError, TypeError, OSError) as exc:
        return replace(result, output="Error: execution handoff rejected: " + str(exc), dispatch_failed=True,
                       metadata={"execution_handoff_rejected": True})


def _valid_versions(value: dict, workspace) -> bool:
    access = WorkspaceFileAccess(workspace)
    try:
        return all(target["identity"] == _identity(access, target["path"]) for target in value["targets"])
    except (ValueError, OSError, KeyError, TypeError):
        return False


def _source_confirmation(value: dict, messages: list) -> str:
    owners = [message for message in messages if message.get("role") == "assistant"
              for call in message.get("tool_calls", []) if call.get("id") == value["source_call"]]
    if owners:
        return ("confirmed" if len(owners) == 1 and owners[0].get(MESSAGE_ID_KEY) == value["source_message"]
                else "conflict")
    compacted = any(
        value["source_session"] and message.get(SESSION_ID_KEY) == value["source_session"]
        and value["source_message"] in (message.get(COMPACTION_KEY) or {}).get("head_message_ids", [])
        and any(part.get("type") == "compaction" and part.get("message_id") == message.get(MESSAGE_ID_KEY)
                for part in message.get(PARTS_KEY, []))
        for message in messages)
    return "confirmed" if compacted else "unconfirmed"


def activate_execution_handoff(state, messages, *, workspace, pure_continuation=False) -> None:
    value = getattr(state, "execution_handoff", None)
    if not value:
        return
    try:
        ExecutionHandoff(**{key: item for key, item in value.items() if key != "stale_reason"})
        if (value["kind"] not in {"inspect", "edit", "verify", "blocked"}
                or value["status"] not in {"pending", "performed", "stale"}
                or not isinstance(value["targets"], list) or len(value["targets"]) > 3
                or not isinstance(value["evidence"], list) or len(value["evidence"]) > 5
                or len(_display_handoff(value)) > 1500):
            raise ValueError("invalid persisted handoff")
        valid_task = value["task"] == _task_key(state)
        valid_task = valid_task and value["workspace"] == str(Path(workspace).resolve())
        if messages is not None:
            confirmation = _source_confirmation(value, messages)
            if confirmation == "conflict":
                value["status"] = "stale"
                value["stale_reason"] = "source call/message identity conflicts"
                return
            value["source_confirmed"] = confirmation == "confirmed"
        valid_version = value["status"] != "pending" or _valid_versions(value, workspace)
        if (value["status"] == "pending" and (
                state.verification_failures > value["verification_failures"]
                or value["kind"] == "verify" and value["generation"] != state.mutation_generation)):
            valid_version = False
        if not valid_task or not valid_version:
            value["status"] = "stale"
            value["stale_reason"] = "task or target version changed"
        elif value["epoch"] != state.task_authority_epoch:
            if not value["source_confirmed"] and (pure_continuation or messages is None):
                return  # 已提交但会话尚未确认，保留旧 epoch，不展示或重新绑定。
            if pure_continuation and value["status"] == "pending" and value["source_confirmed"]:
                value["epoch"] = state.task_authority_epoch
            else:
                value["status"] = "stale"
                value["stale_reason"] = "new user instruction"
    except (TypeError, KeyError, ValueError, AttributeError):
        state.execution_handoff = {}


def render_execution_handoff(state, *, workspace) -> str:
    activate_execution_handoff(state, None, workspace=workspace)
    value = getattr(state, "execution_handoff", None)
    return _display_handoff(value) if value and value["status"] == "pending" and value["source_confirmed"] else ""


def observe_execution_handoff(state, dispatched, messages, *, committed_writes: bool) -> None:
    value = getattr(state, "execution_handoff", None)
    if (not value or value.get("status") != "pending" or not value.get("source_confirmed")
            or value["epoch"] != state.task_authority_epoch):
        return
    access = WorkspaceFileAccess(value["workspace"])
    def canonical(path):
        return str(access.display_path(path, write=True).relative_to(access.root))
    try:
        source, owner = _call_owner(messages, value["source_call"])
        if owner[MESSAGE_ID_KEY] != value["source_message"]:
            return
    except ValueError:
        value["source_confirmed"] = _source_confirmation(value, messages) == "confirmed"
        if not value["source_confirmed"]:
            return
        source = -1
    from nz_coder.tools import collect_filesystem_mutation_paths
    from nz_coder.intelligence.verification_planner import verification_command_key

    for _index, call, result in dispatched:
        if not result.executed or result.dispatch_failed or result.tool_input.get("dry_run"):
            continue
        try:
            current, _ = _call_owner(messages, call["id"])
        except ValueError:
            continue
        if current <= source:
            continue
        matched = False
        if value["kind"] == "edit" and committed_writes and result.is_write:
            paths = {canonical(path) for path in collect_filesystem_mutation_paths(result.tool_input)}
            matched = bool(paths & {target["path"] for target in value["targets"]})
        elif value["kind"] == "verify" and result.name == "bash":
            matched = (verification_command_key(result.tool_input.get("command", ""))
                       == verification_command_key(value["verification_command"])
                       and canonical(result.tool_input.get("cwd") or ".") == canonical(value["verification_cwd"])
                       and value["generation"] == state.mutation_generation)
        elif value["kind"] == "inspect" and result.name in {"read_file", "read_symbol"}:
            observation = (result.metadata or {}).get("model_read_observation") or {}
            for target in value["targets"]:
                if canonical(result.tool_input.get("path") or ".") != target["path"]:
                    continue
                if result.name == "read_symbol":
                    matched = (result.tool_input.get("mode", "read") == "read" and bool(observation)
                               and observation.get("symbol") == target["symbol"])
                else:
                    matched = bool(observation) and (not target["symbol"] or (
                        observation.get("complete") is True and observation.get("offset") == 1))
                if matched:
                    break
        if matched:
            value["status"] = "performed"
            return




class LegacyPlanningRuntime:
    """Preserve mature plan behavior while moving ownership out of Runner."""

    def __init__(self, host) -> None:
        self._host = host

    async def generate(self, messages: list) -> None:
        self._required("_bootstrap_task_contract")(messages)
        await self._required("_maybe_generate_plan")(messages)


    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"PlanningRuntime is missing required capability {name}")
        return value
