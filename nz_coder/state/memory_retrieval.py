"""当前记忆候选的检索，以及不改变正文的有界来源观察。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
from typing import TYPE_CHECKING, Callable
import uuid

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.protocol.public_error import PublicInputError, to_public_error

if TYPE_CHECKING:
    from nz_coder.intelligence.semantic import EmbeddingProvider

_HASH = re.compile(r"[0-9a-f]{64}")
_CHECK_STATUSES = frozenset({"matching", "changed", "missing", "unavailable", "not_checked"})
_MAX_EMBEDDINGS_PER_QUERY = 1000
_RANK_FUSION_OFFSET = 60


@dataclass
class SourceCheckBudget:
    max_sources: int = 8
    max_total_bytes: int = 2_097_152
    sources_checked: int = field(default=0, init=False)
    bytes_read: int = field(default=0, init=False)

    def __post_init__(self):
        if any(type(value) is not int or value < 0
               for value in (self.max_sources, self.max_total_bytes)):
            raise ValueError("来源核对预算必须是非负整数")


@dataclass
class SourceCheckContext:
    execution_root: Path | str
    authorize: Callable[[str], bool]
    cancel_event: object = None
    deadline: float | None = None
    budget: SourceCheckBudget | None = None
    _observations: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        if self.budget is None:
            self.budget = SourceCheckBudget()


class _SourceCheckStopped(PublicInputError):
    pass


def _check_running(context):
    if context.cancel_event is not None and context.cancel_event.is_set():
        raise _SourceCheckStopped("cancelled")
    if context.deadline is not None and time.monotonic() >= context.deadline:
        raise _SourceCheckStopped("deadline_exceeded")


def _file_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() == ".":
        return None
    return candidate.as_posix()


def _identity(raw):
    if not isinstance(raw, dict) or raw.get("expected_exists") is not True:
        return None
    digest = raw.get("content_hash")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        return None
    result = {"expected_exists": True, "content_hash": digest}
    for key in ("device", "inode", "size", "mtime_ns"):
        value = raw.get(key)
        if type(value) is not int or value < 0:
            return None
        result[key] = value
    return result


def _baseline_hash(ref):
    if ref.get("binding") != "bound":
        return None
    value = ref.get("observed_fingerprint") or ref.get("content_hash")
    if isinstance(value, dict):
        value = value.get("content_hash")
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def check_sources(source_refs, context: SourceCheckContext | None) -> list[dict]:
    """在本次执行目录和权限内，将记忆里保存的指纹与当前文件逐一比较。"""
    checks = []
    seen = set()
    for ref in source_refs or []:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("kind", "file")
        locator = ref.get("path") or ref.get("locator") or ""
        path = _file_path(locator) if kind == "file" else None
        baseline_hash = _baseline_hash(ref)
        reference_key = (kind, path or str(locator), baseline_hash)
        if reference_key in seen:
            continue
        seen.add(reference_key)
        check = {
            "observation_key": "source-" + uuid.uuid4().hex,
            "observation_kind": "identity_check",
            "kind": kind,
            "locator": path or locator,
            "path": path or locator,
            "observed_at": time.time(),
            "complete": False,
            "status": "not_checked",
        }
        checks.append(check)
        if context is None:
            check["reason"] = "no_source_context"
            continue
        check["execution_root"] = str(Path(context.execution_root).absolute())
        if kind != "file":
            check["reason"] = "unsupported_source_kind"
            continue
        if path is None:
            check.update(status="unavailable", reason="invalid_path")
            continue
        if baseline_hash is None:
            check["reason"] = "no_bound_fingerprint"
            continue

        # 同一路径只读取一次。不同记忆可能保存过不同版本，各自的指纹要分别比较。
        observation = _current_file_observation(context, check["execution_root"], path)
        check.update(observation)
        if observation.get("identity"):
            digest = observation["identity"]["content_hash"]
            check.update(
                content_hash=digest,
                observed_fingerprint=digest,
                status="matching" if digest == baseline_hash else "changed",
            )
    return checks


def _current_file_observation(context, execution_root, path):
    cache_key = (execution_root, path)
    try:
        _check_running(context)
        observation = context._observations.get(cache_key)
        if observation is None:
            budget = context.budget
            if budget.sources_checked >= budget.max_sources:
                return {"status": "not_checked", "reason": "source_budget_exceeded"}
            budget.sources_checked += 1
            observation = _read_source_identity(context, path)
            context._observations[cache_key] = observation
        _check_running(context)
        return observation
    except _SourceCheckStopped as exc:
        return {"status": "not_checked", "reason": to_public_error(exc).message}
    except FileNotFoundError:
        failure = {"status": "missing", "reason": "source_missing"}
    except (OSError, ValueError) as exc:
        failure = {"status": "unavailable", "reason": type(exc).__name__}
    # 同一上下文后续核对这个路径时，也复用这次读取失败的结果。
    context._observations[cache_key] = failure
    return failure


def _read_source_identity(context, path):
    budget = context.budget
    if context.authorize(path) is not True:
        return {"status": "unavailable", "reason": "permission_denied"}
    _check_running(context)
    access = WorkspaceFileAccess(context.execution_root)
    remaining = max(0, budget.max_total_bytes - budget.bytes_read)
    if access.stat(path).size > remaining:
        return {"status": "unavailable", "reason": "byte_budget_exceeded"}

    # 文件在检查大小后可能增长。先预留全部剩余额度，成功后再退还未读的字节。
    budget.bytes_read += remaining
    data, identity = access.read_bytes_with_identity(
        path, maximum=remaining, check_cancelled=lambda: _check_running(context),
    )
    budget.bytes_read -= remaining - len(data)
    _check_running(context)
    return {"identity": asdict(identity), "complete": True, "observed_at": time.time()}


def _call_arguments(call):
    raw = call.get("function", {}).get("arguments", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _visible_evidence(item, *, prefix=""):
    return (item.get(prefix + "authoritative") is not False
            and item.get(prefix + "internal") is not True
            and item.get(prefix + "visible") is not False)


def _resolve_observation(ref, messages, path):
    if ref.get("kind", "file") != "file":
        return None, "unsupported_source_kind"
    call_id = ref.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None, "no_tool_observation"

    # 调用编号可能在不同会话中重复。先找发起调用的消息，再验证它的身份。
    requested_session = ref.get("session_id")
    owners = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if requested_session and message.get("_nz_session_id") != requested_session:
            continue
        calls = [
            call for call in message.get("tool_calls", [])
            if isinstance(call, dict) and call.get("id") == call_id
        ]
        if calls:
            owners.append((message, calls))
    if len(owners) != 1:
        return None, "ambiguous_or_missing_owner"

    owner, calls = owners[0]
    session_id = owner.get("_nz_session_id")
    message_id = owner.get("_nz_message_id")
    requested_message = ref.get("message_id")
    if (len(calls) != 1 or not session_id or not message_id
            or not _visible_evidence(owner, prefix="_nz_")
            or (requested_message and requested_message != message_id)):
        return None, "ambiguous_or_invalid_owner"

    # 即使另一条消息没有发起调用，它也不能与发起消息共用同一个会话内编号。
    owner_id_count = sum(
        message.get("_nz_session_id") == session_id and message.get("_nz_message_id") == message_id
        for message in messages if isinstance(message, dict)
    )
    if owner_id_count != 1:
        return None, "ambiguous_or_invalid_owner"
    call = calls[0]
    tool = call.get("function", {}).get("name")
    if tool not in {"read_file", "recall_memory"}:
        return None, "unsupported_observation_tool"

    parts = [
        part for part in owner.get("_nz_parts", [])
        if isinstance(part, dict) and part.get("call_id") == call_id
    ]
    tool_messages = [
        message for message in messages if isinstance(message, dict)
        and message.get("role") == "tool" and message.get("tool_call_id") == call_id
        and message.get("_nz_session_id") == session_id
    ]
    if len(parts) != 1 or len(tool_messages) != 1:
        return None, "ambiguous_or_missing_result"
    part = parts[0]
    delivered_message = tool_messages[0]
    if (part.get("type") != "tool" or part.get("tool") != tool
            or not _visible_evidence(part) or not _visible_evidence(delivered_message, prefix="_nz_")):
        return None, "unsettled_or_unseen_result"
    # 执行片段保存运行时的元数据，工具消息保存模型看到的文本，两者必须来自同一次执行。
    run_id = owner.get("_nz_interaction_run_id")
    state = part.get("state", {})
    if (part.get("message_id") != message_id or not run_id or part.get("interaction_run_id") != run_id
            or state.get("status") != "completed" or state.get("output") != delivered_message.get("content")):
        return None, "unsettled_or_unseen_result"
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return None, "no_host_metadata"
    root = (owner.get("_nz_path") or {}).get("root")
    if not isinstance(root, str) or not root:
        return None, "no_execution_root"
    if tool == "read_file":
        observation, reason = _resolve_file_read(path, call, state, delivered_message)
    else:
        observation, reason = _resolve_source_check(
            path, root, ref.get("observation_key"), metadata.get("memory_source_checks"),
        )
    if observation is None:
        return None, reason
    return {
        "session_id": session_id,
        "message_id": message_id,
        "tool_call_id": call_id,
        "execution_root": root,
        **observation,
    }, None


def _resolve_file_read(path, call, state, delivered_message):
    metadata = state["metadata"]
    file_read = metadata.get("model_read_observation")
    if (not isinstance(file_read, dict) or metadata.get("read_cache_hit")
            or delivered_message.get("_nz_evidence_kind") != "file_read" or not path):
        return None, "unbound_file_observation"
    evidence_paths = (
        file_read.get("path"),
        _call_arguments(call).get("path"),
        (state.get("input") or {}).get("path"),
        delivered_message.get("_nz_resource"),
    )
    if any(_file_path(value) != path for value in evidence_paths):
        return None, "unbound_file_observation"
    identity = _identity(file_read.get("identity"))
    offset = file_read.get("offset")
    limit = file_read.get("limit")
    if (identity is None or type(offset) is not int or offset < 1
            or type(limit) is not int or limit < 1 or type(file_read.get("complete")) is not bool):
        return None, "invalid_file_observation"

    # 指纹覆盖整份文件。模型可能只读了几行，返回给它的文本也可能被截短。
    complete = (file_read["complete"] and offset == 1 and not metadata.get("truncated")
                and not (metadata.get("projection") or {}).get("truncated"))
    return {
        "identity": identity, "content_hash": identity["content_hash"],
        "observed_fingerprint": identity["content_hash"], "path": path,
        "observation_kind": "content_read", "offset": offset, "limit": limit,
        "complete": bool(complete),
    }, None


def _resolve_source_check(path, root, key, observations):
    if not key or not isinstance(observations, list):
        return None, "no_source_check_observation"
    matching = [
        item for item in observations
        if isinstance(item, dict) and item.get("observation_key") == key
    ]
    if len(matching) != 1:
        return None, "ambiguous_or_missing_observation_key"
    check = matching[0]
    status = check.get("status")
    if (check.get("observation_kind") != "identity_check" or not path
            or _file_path(check.get("path")) != path or check.get("execution_root") != root
            or status not in _CHECK_STATUSES):
        return None, "invalid_source_check_observation"
    observation = {"path": path, "observation_key": key,
                   "observation_kind": "identity_check", "status": status,
                   "complete": False}
    for field_name in ("reason", "observed_at"):
        if field_name in check:
            observation[field_name] = check[field_name]
    # 文件缺失等结果也能作为这次核对的记录。成功比对过内容时才有指纹。
    if status in {"matching", "changed"}:
        identity = _identity(check.get("identity"))
        if identity is None or check.get("complete") is not True:
            return None, "incomplete_source_identity"
        observation.update(identity=identity, content_hash=identity["content_hash"],
                           observed_fingerprint=identity["content_hash"], complete=True)
    return observation, None


def resolve_source_refs(refs, messages) -> list[dict]:
    """按输入顺序绑定引用。定位信息来自引用，观察结果取自模型可见的已完成工具调用。"""
    results = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("kind", "file")
        locator = ref.get("locator") or ref.get("path") or ""
        path = _file_path(ref.get("path") or locator) if kind == "file" else None
        result = {"kind": kind, "locator": path or locator, "binding": "unbound"}
        if path:
            result["path"] = path
        observation, reason = _resolve_observation(ref, messages or [], path)
        if observation is None:
            result["binding_reason"] = reason
        else:
            result.update(observation, binding="bound")
        results.append(result)
    return results


def hybrid_search(store, query: str, provider: EmbeddingProvider | None, limit: int = 5) -> dict:
    """分别按文字和语义查找记忆，再合并属于同一版本的排名。"""
    limit = max(0, min(int(limit), 1000))
    result = {
        "items": [],
        "semantic_status": "not_configured",
        "encoder_id": None,
        "scan_limited": False,
    }
    if not limit or not str(query).strip():
        return result

    candidate_limit = max(20, limit * 4)
    snapshot = store.search_snapshot(query, limit=min(1000, candidate_limit))
    result["generation"] = tuple(snapshot["generation"])
    lexical_matches = snapshot["items"]
    semantic_matches = []
    if provider is not None:
        semantic_matches, diagnostics = _search_embeddings(store, query, provider, candidate_limit)
        result.update(diagnostics)

    # 编码期间记忆可能被修改或删除。版本有变动时，重新读取一次完整的文字检索结果。
    if tuple(store.generation()) != result["generation"]:
        fresh = store.search_snapshot(query, limit=limit)
        result.update(items=fresh["items"], generation=tuple(fresh["generation"]), semantic_status="source_changed")
        return result

    result["items"] = _merge_rankings(query, lexical_matches, semantic_matches, limit)
    return result


def _search_embeddings(store, query, provider, candidate_limit) -> tuple[list, dict]:
    diagnostics = {"encoder_id": str(provider.identity), "semantic_status": "unavailable"}
    if not _provider_ready(provider):
        return [], diagnostics

    matches = []
    try:
        # 多取一条用来判断是否触及扫描上限，这一条不参与打分。
        candidates = store.embeddings(provider.identity, limit=_MAX_EMBEDDINGS_PER_QUERY + 1)
        diagnostics["scan_limited"] = len(candidates) > _MAX_EMBEDDINGS_PER_QUERY
        if not candidates:
            diagnostics["semantic_status"] = "not_indexed"
            return [], diagnostics

        vectors = provider.embed([str(query)])
        if len(vectors) != 1:
            raise ValueError("查询编码器没有返回一个向量")
        query_vector = _normalized(vectors[0])
        dimensions = len(query_vector)
        has_compatible_vector = False
        scored = []
        for record in candidates[:_MAX_EMBEDDINGS_PER_QUERY]:
            vector = _normalized(record["vector"])
            if len(vector) != dimensions or record["dimensions"] != dimensions:
                continue
            has_compatible_vector = True
            similarity = sum(left * right for left, right in zip(query_vector, vector))
            if similarity > 0:
                scored.append((similarity, record))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        matches = [record for _, record in scored[:candidate_limit]]

        records_missing_embeddings = store.embedding_candidates(provider.identity, limit=1)
        if not has_compatible_vector:
            diagnostics["semantic_status"] = "dimension_mismatch"
        elif records_missing_embeddings:
            diagnostics["semantic_status"] = "partial"
        else:
            diagnostics["semantic_status"] = "ready"
    except Exception as exc:
        # 语义检索是可选的，错误类型随结果返回，文字检索仍可用。
        diagnostics.update(semantic_status="unavailable", semantic_error=type(exc).__name__)
    return matches, diagnostics


def _merge_rankings(query, lexical_matches, semantic_matches, limit) -> list[dict]:
    """同一版本可以累计两份排名的分数，名称或编号完全匹配的记录优先。"""
    records = {}
    scores = {}
    sources = {}
    for label, candidates in (("fts", lexical_matches), ("semantic", semantic_matches)):
        for rank, record in enumerate(candidates, start=1):
            memory_id = record["id"]
            prior = records.get(memory_id)
            if prior is not None and prior.get("revision") != record.get("revision"):
                continue
            records.setdefault(memory_id, record)
            # 全文分数和向量相似度的量纲不同，用倒数排名融合两份结果。
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_RANK_FUSION_OFFSET + rank)
            sources.setdefault(memory_id, []).append(label)

    def sort_key(memory_id):
        exact_match = memory_id == query or records[memory_id].get("name") == query
        return (not exact_match, -scores[memory_id], memory_id)

    matches = []
    for memory_id in sorted(records, key=sort_key)[:limit]:
        record = {
            key: value for key, value in records[memory_id].items()
            if key not in {"vector", "dimensions", "encoder_id", "score"}
        }
        record.update(retrieval_score=scores[memory_id], retrieval_sources=sources[memory_id])
        matches.append(record)
    return matches


def _provider_ready(provider):
    return (bool(str(provider.identity).strip())
            and getattr(provider, "available", True) is True)


def _normalized(vector):
    values = tuple(float(value) for value in vector)
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("向量必须包含有限数值")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("向量范数必须大于零且有限")
    return tuple(value / norm for value in values)


def reindex_memory(store, provider: EmbeddingProvider | None, limit: int = 50) -> dict:
    """仅显式编码缺失版本；计算在事务外，保存时由 store 再次校验版本。"""
    result = {"status": "unavailable", "indexed": 0, "skipped": 0,
              "encoder_id": str(provider.identity) if provider is not None else None}
    if provider is None or not _provider_ready(provider):
        return result
    limit = max(0, min(int(limit), 1000))
    if not limit:
        result["status"] = "ready"
        return result
    try:
        candidates = store.embedding_candidates(provider.identity, limit=limit)
        if not candidates:
            result["status"] = "ready"
            return result
        texts = ["\n".join(str(record.get(key, "")) for key in
                            ("name", "type", "description", "content", "applicability"))
                 for record in candidates]
        vectors = provider.embed(texts)
        if len(vectors) != len(candidates):
            raise ValueError("编码器返回的向量数量与记录数不同")
        normalized = [_normalized(vector) for vector in vectors]
        if len({len(vector) for vector in normalized}) != 1:
            raise ValueError("同批记录的向量维度不同")
        for record, vector in zip(candidates, normalized):
            saved = store.save_embedding(record["id"], record["revision"], record["content_hash"],
                                         provider.identity, vector)
            result["indexed" if saved else "skipped"] += 1
        result["status"] = "partial" if result["skipped"] else "ready"
    except Exception as exc:
        result.update(status="error", error=type(exc).__name__)
    return result
