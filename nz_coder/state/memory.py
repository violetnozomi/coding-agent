"""项目的长期记忆入口。模型工具和宿主通过 MemoryManager 读写同一份记录。"""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from contextvars import ContextVar
import hashlib
from itertools import chain
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import weakref

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY
from nz_coder.protocol.public_error import PublicError, PublicInputError, PublicRuntimeError, to_public_error
from nz_coder.state.memory_scope import MemoryScope, current_memory_scope
from nz_coder.state.workdir import current_derived_path, current_workdir
from nz_coder.tools import ToolOutput, current_tool_call_id, current_tool_cancel_event, register

MEMORY_TYPES = ("user", "project", "feedback", "reference")
_STORE_CONTEXT_FIELDS = {
    "scope_id", "capability", "actor_id", "session_id", "parent_session_id", "task_id",
    "operation_namespace", "operation_id", "cancel_event", "deadline",
}
_RUNTIME: ContextVar[dict | None] = ContextVar("memory_operation_context", default=None)
_MEMORY_MANAGER: ContextVar[MemoryManager | None] = ContextVar("memory_manager", default=None)
_MANAGERS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_MANAGER_LOCK = threading.Lock()


class MemoryUnavailable(PublicRuntimeError):
    def __init__(self, message):
        super().__init__(PublicError("memory_unavailable", message))


@contextmanager
def bind_memory_runtime(**context):
    """同一项目的多个会话共用记忆库，每次执行的身份和来源绑定在当前上下文中。"""
    token = _RUNTIME.set(context)
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def _operation_message_id(messages, call_id) -> str:
    if not call_id:
        return "user-operation"

    # 同一次工具调用重放时要取回原收据。只有唯一的发起消息才能确定这次操作的身份。
    owners = [
        message
        for message in messages
        if message.get("role") == "assistant"
        and any(call.get("id") == call_id for call in message.get("tool_calls", []))
    ]
    if len(owners) != 1 or not owners[0].get(MESSAGE_ID_KEY):
        raise PublicInputError("Memory operation source call identity is missing or ambiguous")
    return str(owners[0][MESSAGE_ID_KEY])


def _operation_deadline(context: dict) -> float:
    """记忆操作最多占用 30 秒，调用方的截止时间可以更早。"""
    local_deadline = time.monotonic() + 30.0
    parent_deadline = context.get("deadline")
    if parent_deadline:
        return min(parent_deadline, local_deadline)
    return local_deadline


def _operation_context(manager):
    from nz_coder.runtime.core.run_context import current_run_context

    context = dict(_RUNTIME.get() or {})
    run = current_run_context()
    if run is not None:
        messages = run.transcript
        session_id = run.session.session_id
    else:
        messages = context.get("messages", [])
        session_id = str(context.get("session_id") or "local-user")

    call_id = current_tool_call_id()
    message_id = _operation_message_id(messages, call_id)
    context.update(
        scope_id=manager.scope_id,
        session_id=session_id,
        actor_id=str(context.get("agent_id") or session_id),
        operation_namespace=f"{manager.scope_id}:{session_id}:{message_id}",
        operation_id=call_id or uuid.uuid4().hex,
        cancel_event=current_tool_cancel_event(),
        deadline=_operation_deadline(context),
    )
    context.setdefault("capability", "manage")
    return context, messages


def _migration_committed(database: Path) -> bool:
    # 迁移中断也会留下数据库文件。只读检查迁移事务提交的标记，避免误开一个空库。
    if not database.exists():
        return False
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT value FROM memory_meta WHERE key='ready'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == "1"


class MemoryManager:
    """提供单个项目的记忆读写、检索和提示词生成。记录及其版本由数据库保存。"""

    def __init__(self, memory_dir: Path | None = None, *, scope_id: str = "", embedding_provider=None):
        self.memory_dir = Path(memory_dir or current_derived_path("MEMORY_DIR")).resolve()
        self.scope_id = scope_id or hashlib.sha256(str(self.memory_dir).encode()).hexdigest()
        self._store = None
        self.embedding_provider = embedding_provider
        self._open_lock = threading.RLock()

    @property
    def store(self):
        from nz_coder.state.memory_migration import has_legacy
        from nz_coder.state.memory_store import SQLiteMemoryStore

        with self._open_lock:
            if self._store is not None:
                return self._store
            if has_legacy(self.memory_dir) and not _migration_committed(self.memory_dir / "memory.sqlite3"):
                raise MemoryUnavailable(
                    "Legacy memory requires explicit 'nz-coder memory migrate' after stopping old writers"
                )
            self._store = SQLiteMemoryStore(self.memory_dir, scope_id=self.scope_id)
            return self._store

    def load_all(self):
        """宿主启动时只检查数据库能否读取，记忆正文等到检索时再加载。"""
        try:
            self.generation()
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            return {"status": "unavailable", "error": to_public_error(exc).message}
        return {"status": "ready"}

    def generation(self):
        return self.store.generation()

    def has_memories(self):
        return bool(self.store.list_records(limit=1)["items"])

    def list_records(self, *, type=None, status="active", limit=50, cursor=None):
        return self.store.list_records(type=type, status=status, limit=limit, cursor=cursor)

    def search_result(self, query, *, limit=5, embedding_provider=None):
        provider = embedding_provider or self.embedding_provider or configured_embedding_provider()
        if provider is not None:
            from nz_coder.state.memory_retrieval import hybrid_search

            return hybrid_search(self.store, str(query), provider, limit)

        from nz_coder.runtime.core.run_settings import current_run_settings

        snapshot = self.store.search_snapshot(str(query), limit=limit)
        hybrid_requested = current_run_settings().memory_retrieval_mode == "hybrid"
        return {**snapshot, "semantic_status": "unavailable" if hybrid_requested else "lexical"}

    def search(self, query, *, limit=5, filters=None):
        items = self.search_result(query, limit=limit)["items"]
        if filters:
            items = [
                record for record in items
                if all(record.get(key) == value for key, value in filters.items())
            ]
        return items

    def read(self, memory_id, *, revision=None, check_sources=False, source_context=None):
        record = self.store.read(str(memory_id), revision=revision)
        if record is not None and check_sources:
            from nz_coder.state.memory_retrieval import check_sources as check_record_sources

            record["source_checks"] = check_record_sources(record.get("source_refs", []), source_context)
        return record

    def history(self, memory_id):
        return self.store.history(memory_id)

    def proposals(self, *, filters=None, limit=50, cursor=None, trusted_context=None):
        context = trusted_context or _operation_context(self)[0]
        proposal_filters = dict(filters or {})
        # 子任务只能查看自己会话提交的提案，调用方传入的过滤条件不能扩大可见范围。
        if context.get("capability") != "manage":
            proposal_filters["source_session"] = context.get("session_id", "")
        return self.store.proposals(filters=proposal_filters, limit=limit, cursor=cursor)

    def read_proposal(self, proposal_id, *, trusted_context=None):
        context = trusted_context or _operation_context(self)[0]
        proposal = self.store.read_proposal(proposal_id)
        if (
            proposal
            and context.get("capability") != "manage"
            and proposal.get("source_session") != context.get("session_id")
        ):
            return None
        return proposal

    def mutate(self, command, *, trusted_context=None):
        try:
            if trusted_context is None:
                context, messages = _operation_context(self)
            else:
                context = dict(trusted_context)
                messages = []
            command = dict(command)

            # 只有会话里真实发生的工具观察才能绑定为来源，模型提交的引用要先核对。
            from nz_coder.state.memory_retrieval import resolve_source_refs

            for field in ("source_refs", "evidence_refs"):
                if field not in command:
                    continue
                references = command[field]
                if not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references):
                    raise PublicInputError(f"{field} must be an array of reference objects")
                command[field] = resolve_source_refs(references, messages)

            # 存储层只需要操作身份、写入权限和取消期限。对话全文和工作目录留在本次执行中。
            store_context = {key: value for key, value in context.items() if key in _STORE_CONTEXT_FIELDS}
            return self.store.mutate(command, store_context)
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            return {"status": "unavailable", "error": to_public_error(exc).message}
        except (ValueError, TypeError) as exc:
            return {"status": "error", "error": to_public_error(exc).message}

    def list_memories(self, type=None):
        page = self.list_records(type=type)
        return json.dumps(page, ensure_ascii=False)

    def build_prompt_block(self, **kwargs):
        return self.build_prompt_snapshot(**kwargs)["block"]

    def build_prompt_snapshot(
        self,
        *,
        query=None,
        max_items=5,
        max_chars=2000,
        rerank_client=None,
        model=None,
        rerank_provider=None,
        rerank_capabilities=None,
        rerank_observer=None,
        rerank_runtime=None,
        embedding_provider=None,
    ) -> dict:
        """生成背景提示，并返回这些记录的数据库版本，供调用方判断缓存是否仍有效。"""
        if query:
            snapshot = self.search_result(query, limit=max_items * 3, embedding_provider=embedding_provider)
        else:
            snapshot = self.store.search_snapshot(None, limit=max_items)

        # 检索期间其他会话可能写入，这里必须使用读取记录时的版本。
        generation = tuple(snapshot["generation"])
        records = snapshot["items"]
        if not records:
            return {"block": "", "generation": generation}
        if rerank_client is not None and model:
            records = rerank_memories(
                query or "", records,
                client=rerank_client, model=model, top_k=max_items,
                provider=rerank_provider, capabilities=rerank_capabilities,
                observer=rerank_observer, model_runtime=rerank_runtime,
            )
        block = _format_prompt_block(records[:max_items], snapshot.get("semantic_status"), max_chars)
        return {"block": block, "generation": generation}


def _format_prompt_block(records: list, semantic_status: str | None, max_chars: int) -> str:
    header = (
        "<long-term-memory>\nBackground notes may be outdated; current user requirements and execution facts take precedence. "
        "Sources: not_checked for this request. Read by ID when an action depends on a note.\n"
    )
    if semantic_status not in {None, "lexical", "ready"}:
        header += f"Semantic retrieval: {semantic_status}; lexical fallback available.\n"
    footer = "</long-term-memory>"
    remaining = max_chars - len(header) - len(footer)
    rows = []
    for record in records:
        memory_id = record.get("id", record.get("memory_id", ""))
        reference = f"{memory_id}@{record['revision']}"
        row = _format_prompt_entry(record, reference)
        if len(row) > remaining:
            # 截断可能丢掉适用条件或争议信息。放不下整条时，只给出可回读的记录编号和版本。
            row = f"- {reference}: details omitted to preserve conditions/warnings; recall by ID.\n"
        if len(row) <= remaining:
            rows.append(row)
            remaining -= len(row)
    if not rows:
        return ""
    return header + "".join(rows) + footer


def _format_prompt_entry(record: dict, reference: str) -> str:
    conditions = str(record.get("applicability") or "unspecified")
    has_bound_source = any(source.get("binding") == "bound" for source in record.get("source_refs", []))
    binding = "bound" if has_bound_source else "unbound"
    row = (
        f"- {reference} {record['name']} [{record['type']}] "
        f"Conditions: {conditions}; source_binding={binding}."
    )
    assessments = [item for item in record.get("assessments", []) if not item.get("resolved")]
    if assessments:
        row += " Assessments (model claims, version-specific): " + json.dumps(assessments, ensure_ascii=False, default=str)
    conflicts = [item for item in record.get("related_records", []) if not item.get("resolved")]
    if conflicts:
        row += " Conflicting/history references: " + json.dumps(conflicts, ensure_ascii=False, default=str)
    return row + f"\nDescription: {record['description']}\n"


def current_memory_manager():
    return _MEMORY_MANAGER.get() or workspace_memory_manager()


def workspace_memory_manager(memory_dir=None, *, scope=None):
    """按存储目录和项目身份复用管理器。子任务在自己的工作树里也能访问父项目的记忆。"""
    scope = scope or current_memory_scope()
    if scope is None and (
        memory_dir is None or Path(memory_dir).resolve() == current_derived_path("MEMORY_DIR").resolve()
    ):
        scope = MemoryScope.for_workspace(current_workdir())

    if memory_dir:
        directory = Path(memory_dir)
    elif scope is not None:
        directory = scope.directory(current_workdir())
    else:
        directory = current_derived_path("MEMORY_DIR")
    directory = directory.resolve()
    scope_id = scope.scope_id if scope is not None else ""
    key = (str(directory), scope_id)
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = MemoryManager(directory, scope_id=scope_id)
            _MANAGERS[key] = manager
        return manager


@contextmanager
def bind_memory_manager(manager):
    token = _MEMORY_MANAGER.set(manager)
    try:
        yield manager
    finally:
        _MEMORY_MANAGER.reset(token)


def _tool_output(value, *, metadata=None):
    return ToolOutput(json.dumps(value, ensure_ascii=False, default=str), metadata=metadata)


def _save_memory(action, **kwargs):
    return _tool_output(current_memory_manager().mutate({"action": action, **kwargs}))


def _delete_memory(memory_id, expected_revision):
    command = {"action": "delete", "memory_id": memory_id, "expected_revision": expected_revision}
    return _tool_output(current_memory_manager().mutate(command))


def _list_memories(kind="records", type=None, status=None, limit=50, cursor=None, filters=None):
    manager = current_memory_manager()
    try:
        if kind == "proposals":
            value = manager.proposals(filters=filters, limit=limit, cursor=cursor)
        elif kind == "records":
            value = manager.list_records(type=type, status=status or "active", limit=limit, cursor=cursor)
        else:
            raise PublicInputError("kind must be records or proposals")
        return _tool_output(value)
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as exc:
        return _tool_output({"status": "unavailable", "error": to_public_error(exc).message})


def local_embedding_provider(model_path):
    """只从已有的本地目录加载编码器，避免启动记忆检索时触发下载。"""
    from nz_coder.intelligence.semantic import sentence_transformer_provider

    path = Path(model_path).expanduser()
    if not path.is_dir():
        return None
    provider = sentence_transformer_provider(str(path.resolve()))
    if provider.status == "configured":
        try:
            provider.prepare_local()
        except RuntimeError:
            # 编码器会记录加载失败的状态，检索仍能使用全文索引。
            pass
    return provider


def configured_embedding_provider():
    from nz_coder.runtime.core.run_settings import current_run_settings

    settings = current_run_settings()
    if settings.memory_retrieval_mode != "hybrid" or not settings.memory_embedding_model:
        return None
    return local_embedding_provider(settings.memory_embedding_model)


def _source_context():
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.state.memory_retrieval import SourceCheckContext

    context = _RUNTIME.get()
    if context is None or "execution_root" not in context:
        return None
    execution_root = Path(context["execution_root"])
    policy = WorkspacePathPolicy(execution_root)

    def authorize(path):
        # 即使记忆来自父项目，核对来源也必须服从当前工作树的路径限制和读取权限。
        try:
            policy.validate_model_read(path)
            authorizer = context.get("source_authorizer")
            return authorizer is not None and authorizer(path) is True
        except (OSError, ValueError):
            return False

    return SourceCheckContext(
        execution_root=execution_root,
        authorize=authorize,
        cancel_event=current_tool_cancel_event(),
        deadline=_operation_deadline(context),
    )


def _recall_memory(kind="record", query=None, id=None, top_k=5, revision=None, check_sources=False):
    manager = current_memory_manager()
    try:
        if (query is None) == (id is None):
            raise PublicInputError("Provide exactly one of query or id")
        if kind == "proposal":
            if id is None or check_sources or revision is not None:
                raise PublicInputError("Proposals are read by id without source checks")
            return _tool_output(manager.read_proposal(id))
        if kind != "record":
            raise PublicInputError("kind must be record or proposal")
        if id:
            record = manager.read(id, revision=revision)
            records = [record]
            result = {"record": record} if check_sources else record
        else:
            result = manager.search_result(query, limit=top_k)
            records = result["items"]

        observations = []
        if check_sources:
            from nz_coder.state.memory_retrieval import check_sources as check_record_sources

            sources = []
            for record in records:
                if record is not None:
                    sources.extend(record.get("source_refs", []))
            observations = check_record_sources(sources, _source_context())
            result = {**result, "source_checks": observations}
        # 模型看到核对结果。运行时保留同一批观察，用来核实后续写入引用的证据。
        return _tool_output(result, metadata={"memory_source_checks": observations})
    except (RuntimeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return _tool_output({"status": "unavailable", "error": to_public_error(exc).message})


# 以下字段是模型调用工具时能看到的接口，来源引用格式由写入和证据核对共用。
_reference_schema = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["file", "message", "document", "tool"]},
        "locator": {"type": "string", "description": "Project-relative path, message reference, or document locator."},
        "path": {"type": "string", "description": "Optional project-relative file path."},
        "tool_call_id": {"type": "string", "description": "A real settled read_file or recall_memory call."},
        "observation_key": {"type": "string", "description": "For a source check, the returned host observation key."},
        "session_id": {"type": "string"},
        "message_id": {"type": "string"},
    },
    "required": ["kind", "locator"],
    "additionalProperties": False,
}
_judgment_schema = {
    "type": "object",
    "properties": {
        "coverage": {"type": "string", "enum": ["whole", "partial"]},
        "covered_claims": {
            "type": "string",
            "description": "For partial coverage, specify exactly which claims were checked.",
        },
        "result": {"type": "string", "enum": ["supported", "contradicted", "inconclusive"]},
        "assessment": {
            "type": "string",
            "description": "Your judgment and reasoning; identity checks alone do not prove claims.",
        },
    },
    "required": ["coverage", "result", "assessment"],
    "additionalProperties": False,
}
_related_schema = {
    "type": "object",
    "properties": {"memory_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1}},
    "required": ["memory_id", "expected_revision"],
    "additionalProperties": False,
}
_save_properties = {
    "action": {
        "type": "string",
        "enum": ["create", "update", "archive", "restore", "assess", "apply_proposal", "reject_proposal"],
    },
    "memory_id": {"type": "string"},
    "expected_revision": {"type": "integer"},
    "name": {"type": "string"},
    "description": {"type": "string"},
    "type": {"type": "string", "enum": list(MEMORY_TYPES)},
    "content": {"type": "string"},
    "applicability": {"type": "string"},
    "source_refs": {"type": "array", "items": _reference_schema},
    "evidence_refs": {"type": "array", "items": _reference_schema},
    "judgment": _judgment_schema,
    "related_records": {"type": "array", "items": _related_schema},
    "resolves_event_ids": {"type": "array", "items": {"type": "string"}},
    "resolution_reason": {"type": "string"},
    "reason": {"type": "string"},
    "proposal_id": {"type": "string"},
    "expected_proposal_revision": {"type": "integer"},
}
register(
    name="save_memory",
    description=(
        "Maintain cross-session project knowledge, not task progress. Search existing notes before choosing create or update. "
        "Use update with ID/expected_revision when new evidence corrects a conclusion; preserve distinct applicability conditions. "
        "assess records whole/partial supported/contradicted/inconclusive model judgments; it does not prove correctness. "
        "Explicit resolves_event_ids withdraw old assessments/conflicts; a new supported claim alone does not settle them. "
        "Bind source_refs/evidence_refs to real tool_call_id/observation_key; never invent fingerprints. "
        "Archive retired context; delete only for explicit removal. Writing children submit proposals only."
    ),
    parameters={
        "type": "object",
        "properties": _save_properties,
        "required": ["action"],
        "additionalProperties": False,
    },
    handler=_save_memory,
)
register(
    name="recall_memory",
    description=(
        "Search long-term background by query or read a known record/proposal ID. Notes may be outdated. "
        "check_sources explicitly compares a bounded set of recorded files in this execution workspace; "
        "matching bytes do not verify conclusions. Read current code when semantic changes matter."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["record", "proposal"]},
            "query": {"type": "string"},
            "id": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
            "revision": {"type": "integer"},
            "check_sources": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    handler=_recall_memory,
    side_effect="readonly",
)
register(
    name="list_memories",
    description="List current records or pending proposals, with pagination; read details by ID.",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["records", "proposals"]},
            "type": {"type": "string", "enum": list(MEMORY_TYPES)},
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": {"type": "string"},
            "filters": {"type": "object"},
        },
        "additionalProperties": False,
    },
    handler=_list_memories,
    side_effect="readonly",
)
register(
    name="delete_memory",
    description="Delete a record and its stored versions by ID and the exact revision you read. This is not archival.",
    parameters={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}, "expected_revision": {"type": "integer"}},
        "required": ["memory_id", "expected_revision"],
        "additionalProperties": False,
    },
    handler=_delete_memory,
)


def _create_chat_completion(
    client,
    *,
    provider=None,
    capabilities=None,
    observer=None,
    model_runtime=None,
    cancel_event=None,
    **kwargs,
):
    """通过模型网关执行记忆请求，沿用调用方的用量观察器和取消信号。"""
    from nz_coder.model_gateway import (
        ModelCall,
        ModelCallPurpose,
        ModelCallStatus,
        ModelSelectionRequest,
        OpenAIClientBridgeProvider,
        ProductionModelGateway,
        resolve_model_runtime,
    )

    model_id = str(kwargs["model"])
    if model_runtime is None:
        active_provider = provider or OpenAIClientBridgeProvider()
        runtime = resolve_model_runtime(ModelSelectionRequest(
            provider_name=str(getattr(active_provider, "name", "openai-compatible")),
            model_id=model_id,
            provider=active_provider,
            client=client,
            owns_client=False,
        ))
    else:
        # 调用方传入的客户端和模型必须与现有运行时匹配，避免用其他任务的模型重排。
        runtime = model_runtime
        active_provider = provider or runtime.provider
        if (
            runtime.client is not client
            or runtime.provider is not active_provider
            or model_id not in {runtime.model_id, runtime.request_model_id}
        ):
            raise PublicInputError("Memory model runtime identity does not match the request")
    if capabilities is not None:
        runtime.capabilities = capabilities
    outcome = ProductionModelGateway(runtime, observer=observer).complete_sync(
        ModelCall(
            purpose=ModelCallPurpose.MEMORY,
            messages=kwargs["messages"],
            max_output_tokens=int(kwargs.get("max_tokens") or 800),
            response_format=kwargs.get("response_format"),
            metadata={"allow_response_format_fallback": True},
            timeout_seconds=600.0,
        ),
        cancel_event=cancel_event,
    )
    if outcome.status is ModelCallStatus.CANCELLED:
        raise asyncio.CancelledError(outcome.error or "memory extraction cancelled")
    if outcome.status is not ModelCallStatus.COMPLETED:
        raise RuntimeError(outcome.error or outcome.status.value)
    return outcome.content


def _parse_json_list(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\])", raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("memories", []) or data.get("items", [])
    return data if isinstance(data, list) else []


def rerank_memories(
    query: str,
    candidates: list[dict],
    client=None,
    model: str | None = None,
    top_k: int = 5,
    *,
    provider=None,
    capabilities=None,
    observer=None,
    model_runtime=None,
) -> list[dict]:
    """用可选的大模型重排记忆，调用失败时沿用检索顺序。"""
    if not candidates or client is None or not model:
        return candidates[:top_k]

    payload = [
        {
            "id": item["id"],
            "name": item["name"],
            "type": item["type"],
            "description": item["description"],
            "content_preview": item.get("content", "")[:500],
        }
        for item in candidates[:10]
    ]
    prompt = (
        "Rerank these coding-agent memories for the query. "
        "Return only JSON: a list of memory IDs, most relevant first. "
        f"Limit to {top_k}.\n"
        f"Query: {query}\n"
        f"Memories: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        raw = _create_chat_completion(
            client,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            provider=provider,
            capabilities=capabilities,
            observer=observer,
            model_runtime=model_runtime,
        )
        model_order = _parse_json_list(raw)
    except Exception:
        return candidates[:top_k]

    candidates_by_id = {item["id"]: item for item in candidates}
    ranked: list[dict] = []
    seen_ids = set()
    # 模型可能漏排或重复返回记录。优先采用它认出的 ID，其余按原始候选顺序补齐。
    for entry in chain(model_order, candidates_by_id):
        memory_id = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(memory_id, str) and memory_id in candidates_by_id and memory_id not in seen_ids:
            seen_ids.add(memory_id)
            ranked.append(candidates_by_id[memory_id])
        if len(ranked) >= top_k:
            break
    return ranked
