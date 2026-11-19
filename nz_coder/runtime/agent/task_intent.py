"""模型任务判断的唯一值、请求来源与版本提交；不裁定授权或验证。"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, is_user_request_message
from nz_coder.protocol.public_error import PublicError, PublicInputError, PublicRuntimeError, to_public_error
from nz_coder.runtime.verification.domain.models import TaskScope


TASK_KINDS = ("bugfix", "feature", "refactor", "test", "review", "explain", "project_creation", "other")
COMPLEXITIES = ("simple", "moderate", "complex", "unknown")


@dataclass(frozen=True)
class AssessmentCallScope:
    task: TaskScope
    interaction_run_id: str
    agent_id: str
    instruction_message_id: str
    authority_epoch: int
    revision: int
    assistant_message_id: str


def parse_task_assessment(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"task_kinds", "complexity", "reason", "uncertainties"}:
        raise PublicInputError("assessment requires task_kinds, complexity, reason and uncertainties")
    kinds = payload["task_kinds"]
    if not isinstance(kinds, list) or any(not isinstance(k, str) or k not in TASK_KINDS for k in kinds):
        raise PublicInputError("invalid task_kinds")
    if not isinstance(payload["complexity"], str) or payload["complexity"] not in COMPLEXITIES:
        raise PublicInputError("invalid semantic complexity")
    if not isinstance(payload["reason"], str):
        raise PublicInputError("assessment reason must be a string")
    questions = payload["uncertainties"]
    if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
        raise PublicInputError("assessment uncertainties must be strings")
    return copy.deepcopy(payload)


def _binding(state, run_context, messages) -> dict:
    domain = run_context.verification
    if domain is None:
        raise ValueError("task assessment requires the current task scope")
    source = next((m for m in reversed(messages) if is_user_request_message(m)), None)
    # system-only调用可正常回答，但没有真实指令就不能发布有归属的判断。
    source_id = source.get(MESSAGE_ID_KEY, "") if source is not None else ""
    return {"task": asdict(domain.scope.task), "instruction_message_id": source_id,
            "authority_epoch": state.task_authority_epoch}


def initialize_task_assessment(state, run_context, messages, *, pure_continuation: bool) -> None:
    binding = _binding(state, run_context, messages)
    record = state.task_assessment_state
    retain = (pure_continuation and bool(binding["instruction_message_id"]) and isinstance(record, dict) and record.get("binding") == binding
              and type(record.get("revision")) is int and record["revision"] >= 0)
    if retain:
        try:
            revision = record["revision"]
            value, receipt = record["value"], record["last_commit"]
            if revision == 0:
                retain = value is None and receipt is None
            else:
                parse_task_assessment(value)
                source = receipt["scope"]
                saved_scope = AssessmentCallScope(task=TaskScope(**source["task"]),
                    **{key: item for key, item in source.items() if key != "task"})
                retain = (
                    saved_scope.task == run_context.verification.scope.task
                    and saved_scope.instruction_message_id == binding["instruction_message_id"]
                    and saved_scope.authority_epoch == binding["authority_epoch"]
                    and saved_scope.revision == revision - 1
                    and bool(saved_scope.interaction_run_id) and bool(saved_scope.assistant_message_id)
                    and isinstance(saved_scope.agent_id, str) and bool(saved_scope.agent_id)
                    and isinstance(receipt["call_id"], str) and bool(receipt["call_id"])
                    and type(receipt["persisted"]) is bool and receipt["revision"] == revision
                    and receipt["payload_hash"] == _payload_hash({**value, "expected_revision": revision - 1})
                )
        except (KeyError, TypeError, ValueError):
            retain = False
    with run_context._state_lock:
        if not retain:
            state.task_assessment_state = {"binding": binding, "revision": 0, "value": None, "last_commit": None}


def current_task_assessment(state) -> dict | None:
    return copy.deepcopy(state.task_assessment_state.get("value"))


def assessment_cache_key(state) -> tuple:
    record = state.task_assessment_state
    return (json.dumps(record.get("binding", {}), sort_keys=True, ensure_ascii=False), record.get("revision", 0))


def render_task_assessment(state) -> str:
    record = state.task_assessment_state
    value = record.get("value")
    return ("<task-assessment>\nModel-authored judgment, not permission or verification evidence. "
            f"expected_revision={record.get('revision', 0)}\n"
            + (json.dumps(value, ensure_ascii=False) if value is not None else "Not published; do not infer a task category.")
            + "\n</task-assessment>")


def capture_assessment_scope(state, run_context, assistant_message_id) -> AssessmentCallScope | None:
    with run_context._state_lock:
        record = state.task_assessment_state
        binding = record["binding"]
        if not binding["instruction_message_id"]:
            return None
        return AssessmentCallScope(TaskScope(**binding["task"]), run_context.interaction_run_id,
            run_context.verification.scope.agent_id, binding["instruction_message_id"], binding["authority_epoch"],
            record["revision"], assistant_message_id)


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _check_source(state, scope, run, superseded) -> None:
    if not isinstance(scope, AssessmentCallScope) or run is None or state._verification_context is not run:
        raise PublicInputError("task assessment request context is unavailable")
    binding = state.task_assessment_state.get("binding", {})
    if (run.cancel_scope.is_set() or run.finalized or run.terminal_status is not None or superseded()
            or scope.interaction_run_id != run.interaction_run_id
            or scope.agent_id != run.verification.scope.agent_id
            or scope.task != run.verification.scope.task
            or binding != {"task": asdict(scope.task), "instruction_message_id": scope.instruction_message_id,
                           "authority_epoch": scope.authority_epoch}
            or state.task_authority_epoch != scope.authority_epoch):
        raise PublicInputError("task assessment request is cancelled, frozen or superseded")


def _commit(state, scope, run, payload, call_id, *, path, superseded) -> dict:
    from nz_coder.runtime.conversation.runtime_checkpoint import build_runtime_checkpoint

    expected = payload["expected_revision"]
    if type(expected) is not int or expected < 0:
        raise PublicInputError("expected_revision must be a nonnegative integer")
    authored = parse_task_assessment({k: v for k, v in payload.items() if k != "expected_revision"})
    fingerprint = _payload_hash(payload)
    # 先持久化候选，再发布内存；写盘期间失效时用当前状态覆盖候选。
    with state._persistence_lock:
        with run._state_lock:
            _check_source(state, scope, run, superseded)
            record = state.task_assessment_state
            receipt = record.get("last_commit")
            if receipt and receipt["scope"] == asdict(scope) and receipt["call_id"] == call_id:
                if receipt["payload_hash"] != fingerprint:
                    raise PublicInputError("same assessment call has a different payload")
                return {"status": "already_accepted", "revision": record["revision"],
                        "persisted": receipt["persisted"], "reason": ""}
            if expected != scope.revision or expected != record["revision"]:
                raise PublicInputError(f"stale assessment revision; expected_revision={record['revision']}")
            candidate = {"binding": copy.deepcopy(record["binding"]), "revision": expected + 1,
                         "value": authored, "last_commit": {
                             "scope": asdict(scope), "call_id": call_id, "payload_hash": fingerprint,
                             "revision": expected + 1, "persisted": path is not None}}
            snapshot = build_runtime_checkpoint(state, active=True)
            snapshot["task_assessment_state"] = candidate
            old_key = assessment_cache_key(state)
        if path is not None:
            state._write_snapshot(path, snapshot)
        failure = None
        with run._state_lock:
            try:
                _check_source(state, scope, run, superseded)
                if assessment_cache_key(state) != old_key:
                    raise PublicInputError("assessment changed during save")
            except ValueError as exc:
                failure = exc
                rollback = build_runtime_checkpoint(state, active=True)
            else:
                state.task_assessment_state = candidate
        if failure is not None:
            if path is not None:
                try:
                    state._write_snapshot(path, rollback)
                except OSError as exc:
                    raise PublicRuntimeError(PublicError("task_assessment_rollback_failed",
                        "Task assessment checkpoint could not be restored.")) from exc
            raise failure
    return {"status": "accepted", "revision": candidate["revision"], "persisted": path is not None, "reason": ""}


def submit_task_assessment(state, scope, call, result, messages, *, run_context, path=None,
                           superseded=lambda: False):
    proposal = (result.metadata or {}).get("task_assessment_proposal")
    if proposal is None or result.dispatch_failed:
        return result
    try:
        if not isinstance(scope, AssessmentCallScope) or run_context is None:
            raise PublicInputError("task assessment request context is unavailable")
        if result.name != "update_task_assessment" or not result.executed or call.get("function", {}).get("name") != result.name:
            raise PublicInputError("only executed update_task_assessment can publish a judgment")
        args = call["function"].get("arguments", {})
        args = json.loads(args) if isinstance(args, str) else args
        if proposal != args or proposal != result.tool_input:
            raise PublicInputError("task assessment proposal differs from the actual tool input")
        owners = [m for m in messages if m.get(MESSAGE_ID_KEY) == scope.assistant_message_id
                  and m.get("role") == "assistant" and any(c.get("id") == call.get("id") for c in m.get("tool_calls", []))]
        if len(owners) != 1 or not call.get("id"):
            raise PublicInputError("task assessment source call is unavailable or ambiguous")
        receipt = _commit(state, scope, run_context, proposal, call["id"], path=path, superseded=superseded)
    except (ValueError, TypeError, OSError) as exc:
        result.dispatch_failed = True
        receipt = {"status": "rejected", "revision": state.task_assessment_state.get("revision", 0),
                   "persisted": False, "reason": to_public_error(exc).message}
        result.output = "Error: task assessment rejected: " + receipt["reason"]
    else:
        result.output = (f"Task assessment {receipt['status']}; expected_revision={receipt['revision']}; "
                         f"persisted={str(receipt['persisted']).lower()}.")
    result.metadata = {**dict(result.metadata or {}), "task_assessment_commit": receipt}
    return result
