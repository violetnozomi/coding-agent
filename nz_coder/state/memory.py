"""跨会话记忆门面：单一事务存储、显式维护和按项目召回。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import weakref

from nz_coder.state.workdir import current_derived_path, current_workdir
from nz_coder.state.memory_scope import current_memory_scope
from nz_coder.tools import ToolOutput, current_tool_call_id, current_tool_cancel_event, register
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY
from nz_coder.protocol.public_error import PublicError, PublicInputError, PublicRuntimeError, to_public_error

MEMORY_TYPES = ("user", "project", "feedback", "reference")
_RUNTIME: ContextVar[dict | None] = ContextVar("memory_operation_context", default=None)
_MEMORY_MANAGER: ContextVar[MemoryManager | None] = ContextVar("memory_manager", default=None)
_MANAGERS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_MANAGER_LOCK = threading.Lock()


class MemoryUnavailable(PublicRuntimeError):
    def __init__(self, message):
        super().__init__(PublicError("memory_unavailable", message))


@contextmanager
def bind_memory_runtime(**context):
    """只绑定当前执行的身份和来源，不将其写进共享manager。"""
    token = _RUNTIME.set(context)
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def _operation_context(manager):
    from nz_coder.runtime.core.run_context import current_run_context
    context = dict(_RUNTIME.get() or {})
    run = current_run_context()
    messages = run.transcript if run is not None else context.get("messages", [])
    session_id = run.session.session_id if run is not None else str(context.get("session_id") or "local-user")
    call_id = current_tool_call_id()
    owners = [m for m in messages if m.get("role") == "assistant" and
              any(c.get("id") == call_id for c in m.get("tool_calls", []))] if call_id else []
    if call_id and (len(owners) != 1 or not owners[0].get(MESSAGE_ID_KEY)):
        raise PublicInputError("Memory operation source call identity is missing or ambiguous")
    message_id = str(owners[0][MESSAGE_ID_KEY]) if owners else "user-operation"
    context.update(scope_id=manager.scope_id, session_id=session_id,
                   actor_id=str(context.get("agent_id") or session_id),
                   operation_namespace=f"{manager.scope_id}:{session_id}:{message_id}",
                   operation_id=call_id or uuid.uuid4().hex)
    context["cancel_event"] = current_tool_cancel_event()
    context["deadline"] = min(context.get("deadline") or float("inf"), time.monotonic() + 30.0)
    context.setdefault("capability", "manage")
    return context, messages


class MemoryManager:
    def __init__(self, memory_dir: Path | None = None, *, scope_id: str = "", embedding_provider=None):
        self.memory_dir = Path(memory_dir or current_derived_path("MEMORY_DIR")).resolve()
        self.scope_id = scope_id or hashlib.sha256(str(self.memory_dir).encode()).hexdigest()
        self._store = None
        self.embedding_provider = embedding_provider
        self._open_lock = threading.RLock()

    @property
    def store(self):
        from nz_coder.state.memory_store import SQLiteMemoryStore
        from nz_coder.state.memory_migration import has_legacy
        with self._open_lock:
            if self._store is None:
                if has_legacy(self.memory_dir):
                    ready = False
                    database = self.memory_dir / "memory.sqlite3"
                    if database.exists():
                        try:
                            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
                                row = conn.execute("SELECT value FROM memory_meta WHERE key='ready'").fetchone()
                                ready = row is not None and row[0] == "1"
                        except sqlite3.Error:
                            # 迁移可能在创建文件后、提交schema前退出；文件存在不等于切换成功。
                            ready = False
                    if not ready:
                        raise MemoryUnavailable("Legacy memory requires explicit 'nz-coder memory migrate' after stopping old writers")
                self._store = SQLiteMemoryStore(self.memory_dir, scope_id=self.scope_id)
            return self._store

    def load_all(self):
        # 沿用宿主初始化入口，但不将所有正文加载成第二份可变真源。
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
        snapshot = self.store.search_snapshot(str(query), limit=limit)
        from nz_coder.runtime.core.run_settings import current_run_settings
        status = "unavailable" if current_run_settings().memory_retrieval_mode == "hybrid" else "lexical"
        return {**snapshot, "semantic_status": status}

    def search(self, query, *, limit=5, filters=None):
        items = self.search_result(query, limit=limit)["items"]
        if filters:
            items = [r for r in items if all(r.get(k) == v for k, v in filters.items())]
        return items

    def read(self, memory_id, *, revision=None, check_sources=False, source_context=None):
        record = self.store.read(str(memory_id), revision=revision)
        if record is not None and check_sources:
            from nz_coder.state.memory_retrieval import check_sources as check
            record["source_checks"] = check(record.get("source_refs", []), source_context)
        return record

    def history(self, memory_id):
        return self.store.history(memory_id)

    def proposals(self, *, filters=None, limit=50, cursor=None, trusted_context=None):
        context = trusted_context or _operation_context(self)[0]
        selected = dict(filters or {})
        if context.get("capability") != "manage":
            selected["source_session"] = context.get("session_id", "")
        return self.store.proposals(filters=selected, limit=limit, cursor=cursor)

    def read_proposal(self, proposal_id, *, trusted_context=None):
        context = trusted_context or _operation_context(self)[0]
        proposal = self.store.read_proposal(proposal_id)
        if proposal and context.get("capability") != "manage" and proposal.get("source_session") != context.get("session_id"):
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
            from nz_coder.state.memory_retrieval import resolve_source_refs
            for field in ("source_refs", "evidence_refs"):
                if field in command:
                    if not isinstance(command[field], list) or any(not isinstance(ref, dict) for ref in command[field]):
                        raise PublicInputError(f"{field} must be an array of reference objects")
                    command[field] = resolve_source_refs(command[field], messages)
            # 传给store的context仅有真实身份；执行目录/消息/取消对象不进入JSON摘要。
            fields = {"capability", "scope_id", "actor_id", "session_id", "parent_session_id", "task_id",
                      "operation_namespace", "operation_id", "cancel_event", "deadline"}
            return self.store.mutate(command, {k: v for k, v in context.items() if k in fields})
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            return {"status": "unavailable", "error": to_public_error(exc).message}
        except (ValueError, TypeError) as exc:
            return {"status": "error", "error": to_public_error(exc).message}

    def list_memories(self, type=None):
        page = self.list_records(type=type)
        return json.dumps(page, ensure_ascii=False)

    def build_prompt_block(self, **kwargs):
        return self.build_prompt_snapshot(**kwargs)["block"]

    def build_prompt_snapshot(self, *, query=None, max_items=5, max_chars=2000,
                           rerank_client=None, model=None, rerank_provider=None,
                           rerank_capabilities=None, rerank_observer=None, rerank_runtime=None, embedding_provider=None):
        projection = (self.search_result(query, limit=max_items * 3, embedding_provider=embedding_provider)
                      if query else self.store.search_snapshot(None, limit=max_items))
        items = projection["items"]
        generation = tuple(projection["generation"])
        if not items:
            return {"block": "", "generation": generation}
        if rerank_client is not None and model:
            items = rerank_memories(query or "", items, client=rerank_client, model=model, top_k=max_items,
                                    provider=rerank_provider, capabilities=rerank_capabilities,
                                    observer=rerank_observer, model_runtime=rerank_runtime)
        header = ("<long-term-memory>\nBackground notes may be outdated; current user requirements and execution facts take precedence. "
                  "Sources: not_checked for this request. Read by ID when an action depends on a note.\n")
        if projection.get("semantic_status") not in {None, "lexical", "ready"}:
            header += f"Semantic retrieval: {projection['semantic_status']}; lexical fallback available.\n"
        rows = []
        used = len(header) + len("</long-term-memory>")
        for item in items[:max_items]:
            identity = item.get("id", item.get("memory_id", ""))
            conditions = str(item.get("applicability") or "unspecified")
            assessments = [a for a in item.get("assessments", []) if not a.get("resolved")]
            related = [a for a in item.get("related_records", []) if not a.get("resolved")]
            caution = ""
            if assessments:
                caution += " Assessments (model claims, version-specific): " + json.dumps(assessments, ensure_ascii=False, default=str)
            if related:
                caution += " Conflicting/history references: " + json.dumps(related, ensure_ascii=False, default=str)
            binding = "bound" if any(s.get("binding") == "bound" for s in item.get("source_refs", [])) else "unbound"
            row = (f"- {identity}@{item['revision']} {item['name']} [{item['type']}] "
                   f"Conditions: {conditions}; source_binding={binding}.{caution}\nDescription: {item['description']}\n")
            if used + len(row) > max_chars:
                omitted = f"- {identity}@{item['revision']}: details omitted to preserve conditions/warnings; recall by ID.\n"
                if used + len(omitted) <= max_chars:
                    rows.append(omitted)
                    used += len(omitted)
                continue
            rows.append(row)
            used += len(row)
        return {"block": header + "".join(rows) + "</long-term-memory>" if rows else "", "generation": generation}


def current_memory_manager():
    return _MEMORY_MANAGER.get() or workspace_memory_manager()


def workspace_memory_manager(memory_dir=None, *, scope=None):
    scope = scope or current_memory_scope()
    if scope is None and (memory_dir is None or Path(memory_dir).resolve() == current_derived_path("MEMORY_DIR").resolve()):
        from nz_coder.state.memory_scope import MemoryScope
        scope = MemoryScope.for_workspace(current_workdir())
    selected = Path(memory_dir or (scope.directory(current_workdir()) if scope else current_derived_path("MEMORY_DIR"))).resolve()
    key = (str(selected), scope.scope_id if scope else "")
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = MemoryManager(selected, scope_id=scope.scope_id if scope else "")
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
    return _tool_output(current_memory_manager().mutate({"action": "delete", "memory_id": memory_id,
                                                        "expected_revision": expected_revision}))


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
    """显式本地路径才允许加载；不把模型名变成下载请求。"""
    from nz_coder.intelligence.semantic import sentence_transformer_provider
    path = Path(model_path).expanduser()
    if not path.is_dir():
        return None
    provider = sentence_transformer_provider(str(path.resolve()))
    if provider.status == "configured":
        try:
            provider.prepare_local()
        except RuntimeError:
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
    root = Path(context["execution_root"])
    policy = WorkspacePathPolicy(root)
    def authorize(path):
        try:
            policy.validate_model_read(path)
            authorizer = context.get("source_authorizer")
            return authorizer is not None and authorizer(path) is True
        except (OSError, ValueError):
            return False
    return SourceCheckContext(execution_root=root, authorize=authorize,
                              cancel_event=current_tool_cancel_event(), deadline=min(context.get("deadline") or float("inf"), time.monotonic() + 30.0))


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
        search = None if id else manager.search_result(query, limit=top_k)
        items = [manager.read(id, revision=revision)] if id else search["items"]
        observations = []
        if check_sources:
            from nz_coder.state.memory_retrieval import check_sources as check
            sources = [s for r in items if r is not None for s in r.get("source_refs", [])]
            observations = check(sources, _source_context())
        value = items[0] if id else search
        if check_sources:
            value = {"record": value, "source_checks": observations} if id else {**value, "source_checks": observations}
        return _tool_output(value, metadata={"memory_source_checks": observations})
    except (RuntimeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return _tool_output({"status": "unavailable", "error": to_public_error(exc).message})


# 仅保留四个模型工具名称，嵌套字段也明确告知模型，不让模型猜维护协议。
_reference_schema = {
    "type": "object", "properties": {
        "kind": {"type": "string", "enum": ["file", "message", "document", "tool"]},
        "locator": {"type": "string", "description": "Project-relative path, message reference, or document locator."},
        "path": {"type": "string", "description": "Optional project-relative file path."},
        "tool_call_id": {"type": "string", "description": "A real settled read_file or recall_memory call."},
        "observation_key": {"type": "string", "description": "For a source check, the returned host observation key."},
        "session_id": {"type": "string"}, "message_id": {"type": "string"},
    }, "required": ["kind", "locator"], "additionalProperties": False,
}
_judgment_schema = {
    "type": "object", "properties": {
        "coverage": {"type": "string", "enum": ["whole", "partial"]},
        "covered_claims": {"type": "string", "description": "For partial coverage, specify exactly which claims were checked."},
        "result": {"type": "string", "enum": ["supported", "contradicted", "inconclusive"]},
        "assessment": {"type": "string", "description": "Your judgment and reasoning; identity checks alone do not prove claims."},
    }, "required": ["coverage", "result", "assessment"], "additionalProperties": False,
}
_related_schema = {
    "type": "object", "properties": {"memory_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1}},
    "required": ["memory_id", "expected_revision"], "additionalProperties": False,
}
_save_properties = {
    "action": {"type": "string", "enum": ["create", "update", "archive", "restore", "assess", "apply_proposal", "reject_proposal"]},
    "memory_id": {"type": "string"}, "expected_revision": {"type": "integer"},
    "name": {"type": "string"}, "description": {"type": "string"},
    "type": {"type": "string", "enum": list(MEMORY_TYPES)}, "content": {"type": "string"},
    "applicability": {"type": "string"}, "source_refs": {"type": "array", "items": _reference_schema},
    "evidence_refs": {"type": "array", "items": _reference_schema}, "judgment": _judgment_schema,
    "related_records": {"type": "array", "items": _related_schema},
    "resolves_event_ids": {"type": "array", "items": {"type": "string"}},
    "resolution_reason": {"type": "string"}, "reason": {"type": "string"},
    "proposal_id": {"type": "string"}, "expected_proposal_revision": {"type": "integer"},
}
register(name="save_memory", description=(
    "Maintain cross-session project knowledge, not task progress. Search existing notes before choosing create or update. "
    "Use update with ID/expected_revision when new evidence corrects a conclusion; preserve distinct applicability conditions. "
    "assess records whole/partial supported/contradicted/inconclusive model judgments; it does not prove correctness. "
    "Explicit resolves_event_ids withdraw old assessments/conflicts; a new supported claim alone does not settle them. "
    "Bind source_refs/evidence_refs to real tool_call_id/observation_key; never invent fingerprints. "
    "Archive retired context; delete only for explicit removal. Writing children submit proposals only."),
    parameters={"type": "object", "properties": _save_properties, "required": ["action"], "additionalProperties": False}, handler=_save_memory)
register(name="recall_memory", description=(
    "Search long-term background by query or read a known record/proposal ID. Notes may be outdated. "
    "check_sources explicitly compares a bounded set of recorded files in this execution workspace; "
    "matching bytes do not verify conclusions. Read current code when semantic changes matter."),
    parameters={"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["record", "proposal"]}, "query": {"type": "string"},
        "id": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        "revision": {"type": "integer"}, "check_sources": {"type": "boolean"}}, "additionalProperties": False},
    handler=_recall_memory, side_effect="readonly")
register(name="list_memories", description="List current records or pending proposals, with pagination; read details by ID.",
    parameters={"type": "object", "properties": {"kind": {"type": "string", "enum": ["records", "proposals"]},
        "type": {"type": "string", "enum": list(MEMORY_TYPES)}, "status": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "string"},
        "filters": {"type": "object"}}, "additionalProperties": False}, handler=_list_memories, side_effect="readonly")
register(name="delete_memory", description="Delete a record and its stored versions by ID and the exact revision you read. This is not archival.",
    parameters={"type": "object", "properties": {"memory_id": {"type": "string"}, "expected_revision": {"type": "integer"}},
                "required": ["memory_id", "expected_revision"], "additionalProperties": False}, handler=_delete_memory)


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
    """优先使用 JSON mode；不支持时回退到普通 completion。"""
    from nz_coder.model_gateway import (
        ModelCall,
        ModelCallPurpose,
        ModelCallStatus,
        ModelSelectionRequest,
        OpenAIClientBridgeProvider,
        ProductionModelGateway,
        resolve_model_runtime,
    )

    active_provider = provider or (model_runtime.provider if model_runtime is not None else OpenAIClientBridgeProvider())
    if model_runtime is not None and (
        model_runtime.client is not client or model_runtime.provider is not active_provider
        or str(kwargs["model"]) not in {model_runtime.model_id, model_runtime.request_model_id}
    ):
        raise PublicInputError("Memory model runtime identity does not match the request")
    runtime = model_runtime or resolve_model_runtime(ModelSelectionRequest(
        provider_name=str(
            getattr(active_provider, "name", "openai-compatible")
        ),
        model_id=str(kwargs["model"]),
        provider=active_provider,
        client=client,
        owns_client=False,
    ))
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
    """用可选 LLM 对粗召回结果重排；失败时保持原排序。"""
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
        names = _parse_json_list(raw)
    except Exception:
        return candidates[:top_k]

    by_id = {item["id"]: item for item in candidates}
    ranked: list[dict] = []
    seen = set()
    for value in [*names, *(item["id"] for item in candidates)]:
        identity = value.get("id") if isinstance(value, dict) else value
        if isinstance(identity, str) and identity in by_id and identity not in seen:
            seen.add(identity)
            ranked.append(by_id[identity])
        if len(ranked) >= top_k:
            break
    return ranked
