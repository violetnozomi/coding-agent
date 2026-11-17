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
    """只观察明确来源；所有文件读取使用本次调用的执行目录与权限。"""
    checks, seen = [], set()
    for ref in source_refs or []:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("kind", "file")
        locator = ref.get("path") or ref.get("locator") or ""
        path = _file_path(locator) if kind == "file" else None
        key = (kind, path or str(locator), _baseline_hash(ref))
        if key in seen:
            continue
        seen.add(key)
        check = {
            "observation_key": "source-" + uuid.uuid4().hex,
            "observation_kind": "identity_check", "kind": kind,
            "locator": path or locator, "path": path or locator,
            "observed_at": time.time(), "complete": False,
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
        baseline = _baseline_hash(ref)
        if baseline is None:
            check["reason"] = "no_bound_fingerprint"
            continue
        try:
            _check_running(context)
            budget = context.budget
            cache_key = (check["execution_root"], path)
            observation = context._observations.get(cache_key)
            if observation is None:
                if budget.sources_checked >= budget.max_sources:
                    check["reason"] = "source_budget_exceeded"
                    continue
                budget.sources_checked += 1
                if context.authorize(path) is not True:
                    observation = {"status": "unavailable", "reason": "permission_denied"}
                else:
                    _check_running(context)
                    access = WorkspaceFileAccess(context.execution_root)
                    remaining = max(0, budget.max_total_bytes - budget.bytes_read)
                    size = access.stat(path).size
                    if size > remaining:
                        observation = {"status": "unavailable", "reason": "byte_budget_exceeded"}
                    else:
                        # stat后文件可能增长。先预留本次可读总额，失败时不复用未知的已消耗预算。
                        budget.bytes_read += remaining
                        data, identity = access.read_bytes_with_identity(
                            path, maximum=remaining,
                            check_cancelled=lambda: _check_running(context),
                        )
                        budget.bytes_read -= remaining - len(data)
                        _check_running(context)
                        observation = {"identity": asdict(identity), "complete": True,
                                       "observed_at": time.time()}
                context._observations[cache_key] = observation
            _check_running(context)
            check.update(observation)
            if check.get("identity"):
                digest = check["identity"]["content_hash"]
                check.update(content_hash=digest, observed_fingerprint=digest,
                             status="matching" if digest == baseline else "changed")
        except _SourceCheckStopped as exc:
            check.update(status="not_checked", reason=to_public_error(exc).message, complete=False)
        except FileNotFoundError:
            check.update(status="missing", reason="source_missing")
            context._observations[cache_key] = {"status": "missing", "reason": "source_missing"}
        except (OSError, ValueError) as exc:
            check.update(status="unavailable", reason=type(exc).__name__)
            context._observations[cache_key] = {"status": "unavailable", "reason": type(exc).__name__}
    return checks


def _arguments(call):
    raw = call.get("function", {}).get("arguments", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _visible(item, *, owner=False):
    prefix = "_nz_" if owner else ""
    return (item.get(prefix + "authoritative") is not False
            and item.get(prefix + "internal") is not True
            and item.get(prefix + "visible") is not False)


def _resolve_observation(ref, messages):
    if ref.get("kind", "file") != "file":
        return None, "unsupported_source_kind"
    call_id = ref.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None, "no_tool_observation"
    owners = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if ref.get("session_id") and message.get("_nz_session_id") != ref["session_id"]:
            continue
        calls = [call for call in message.get("tool_calls", [])
                 if isinstance(call, dict) and call.get("id") == call_id]
        if calls:
            owners.append((message, calls))
    if len(owners) != 1:
        return None, "ambiguous_or_missing_owner"
    owner, calls = owners[0]
    session_id, message_id = owner.get("_nz_session_id"), owner.get("_nz_message_id")
    if (len(calls) != 1 or not session_id or not message_id or not _visible(owner, owner=True)
            or (ref.get("message_id") and ref["message_id"] != message_id)
            or sum(message.get("_nz_session_id") == session_id
                   and message.get("_nz_message_id") == message_id
                   for message in messages if isinstance(message, dict)) != 1):
        return None, "ambiguous_or_invalid_owner"
    call = calls[0]
    tool = call.get("function", {}).get("name")
    if tool not in {"read_file", "recall_memory"}:
        return None, "unsupported_observation_tool"
    parts = [part for part in owner.get("_nz_parts", [])
             if isinstance(part, dict) and part.get("call_id") == call_id]
    results = [message for message in messages if isinstance(message, dict)
               and message.get("role") == "tool" and message.get("tool_call_id") == call_id
               and message.get("_nz_session_id") == session_id]
    if len(parts) != 1 or len(results) != 1:
        return None, "ambiguous_or_missing_result"
    part, result = parts[0], results[0]
    state = part.get("state", {})
    if (part.get("type") != "tool" or part.get("tool") != tool
            or not _visible(part) or not _visible(result, owner=True) or part.get("message_id") != message_id
            or not owner.get("_nz_interaction_run_id")
            or part.get("interaction_run_id") != owner["_nz_interaction_run_id"]
            or state.get("status") != "completed" or state.get("output") != result.get("content")):
        return None, "unsettled_or_unseen_result"
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return None, "no_host_metadata"
    root = (owner.get("_nz_path") or {}).get("root")
    if not isinstance(root, str) or not root:
        return None, "no_execution_root"
    path = _file_path(ref.get("path") or ref.get("locator"))
    common = {"session_id": session_id, "message_id": message_id,
              "tool_call_id": call_id, "execution_root": root}
    if tool == "read_file":
        raw = metadata.get("model_read_observation")
        if (not isinstance(raw, dict) or metadata.get("read_cache_hit")
                or result.get("_nz_evidence_kind") != "file_read"
                or not path or any(_file_path(value) != path for value in (
                    raw.get("path"), _arguments(call).get("path"),
                    (state.get("input") or {}).get("path"), result.get("_nz_resource")))):
            return None, "unbound_file_observation"
        identity = _identity(raw.get("identity"))
        offset, limit = raw.get("offset"), raw.get("limit")
        if (identity is None or type(offset) is not int or offset < 1
                or type(limit) is not int or limit < 1 or type(raw.get("complete")) is not bool):
            return None, "invalid_file_observation"
        complete = (raw["complete"] and offset == 1 and not metadata.get("truncated")
                    and not (metadata.get("projection") or {}).get("truncated"))
        return {**common, "identity": identity, "content_hash": identity["content_hash"],
                "observed_fingerprint": identity["content_hash"], "path": path,
                "observation_kind": "content_read", "offset": offset, "limit": limit,
                "complete": bool(complete)}, None
    key = ref.get("observation_key")
    observations = metadata.get("memory_source_checks")
    if not key or not isinstance(observations, list):
        return None, "no_source_check_observation"
    matching = [item for item in observations if isinstance(item, dict) and item.get("observation_key") == key]
    if len(matching) != 1:
        return None, "ambiguous_or_missing_observation_key"
    raw = matching[0]
    if (raw.get("observation_kind") != "identity_check" or not path
            or _file_path(raw.get("path")) != path or raw.get("execution_root") != root
            or raw.get("status") not in _CHECK_STATUSES):
        return None, "invalid_source_check_observation"
    observation = {**common, "path": path, "observation_key": key,
                   "observation_kind": "identity_check", "status": raw["status"],
                   "complete": False}
    for field_name in ("reason", "observed_at"):
        if field_name in raw:
            observation[field_name] = raw[field_name]
    identity = _identity(raw.get("identity"))
    if raw["status"] in {"matching", "changed"}:
        if identity is None or raw.get("complete") is not True:
            return None, "incomplete_source_identity"
        observation.update(identity=identity, content_hash=identity["content_hash"],
                           observed_fingerprint=identity["content_hash"], complete=True)
    return observation, None


def resolve_source_refs(refs, messages) -> list[dict]:
    """从唯一已落定的宿主metadata绑定来源，忽略模型自报的观察字段。"""
    results = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("kind", "file")
        locator = ref.get("locator") or ref.get("path") or ""
        result = {"kind": kind, "locator": locator, "binding": "unbound"}
        if kind == "file":
            path = _file_path(ref.get("path") or locator)
            if path:
                result.update(path=path, locator=path)
        observation, reason = _resolve_observation(ref, messages or [])
        if observation is None:
            result["binding_reason"] = reason
        else:
            result.update(observation, binding="bound")
        results.append(result)
    return results


def hybrid_search(store, query: str, provider: EmbeddingProvider | None, limit: int = 5) -> dict:
    """融合当前版本的两个名次，不把 BM25 与余弦分值直接相加。"""
    limit = max(0, min(int(limit), 1000))
    result = {"items": [], "semantic_status": "not_configured", "encoder_id": None,
              "scan_limited": False}
    if not limit or not str(query).strip():
        return result
    snapshot = store.search_snapshot(query, limit=min(1000, max(20, limit * 4)))
    result["generation"] = tuple(snapshot["generation"])
    lexical = snapshot["items"]
    semantic = []
    if provider is not None:
        result["encoder_id"] = str(provider.identity)
        if not _provider_ready(provider):
            result["semantic_status"] = "unavailable"
        else:
            try:
                candidates = store.embeddings(provider.identity, limit=1001)
                result["scan_limited"] = len(candidates) > 1000
                if not candidates:
                    result["semantic_status"] = "not_indexed"
                else:
                    vectors = provider.embed([str(query)])
                    if len(vectors) != 1:
                        raise ValueError("查询编码器没有返回一个向量")
                    query_vector = _normalized(vectors[0])
                    compatible = 0
                    scored = []
                    for item in candidates[:1000]:
                        vector = _normalized(item["vector"])
                        if (len(vector) != len(query_vector)
                                or item["dimensions"] != len(query_vector)):
                            continue
                        compatible += 1
                        similarity = sum(left * right for left, right in zip(query_vector, vector))
                        # 零或反向相似不为凑数进入背景材料。
                        if similarity > 0:
                            scored.append((similarity, item))
                    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
                    semantic = [item for _, item in scored[:max(20, limit * 4)]]
                    incomplete = store.embedding_candidates(provider.identity, limit=1)
                    result["semantic_status"] = (
                        "dimension_mismatch" if not compatible else "partial" if incomplete else "ready"
                    )
            except Exception as exc:
                result.update(semantic_status="unavailable", semantic_error=type(exc).__name__)
    if tuple(store.generation()) != result["generation"]:
        fresh = store.search_snapshot(query, limit=limit)
        result.update(items=fresh["items"], generation=tuple(fresh["generation"]), semantic_status="source_changed")
        return result
    records, scores, sources = {}, {}, {}
    for label, candidates in (("fts", lexical), ("semantic", semantic)):
        for rank, record in enumerate(candidates, start=1):
            memory_id = record["id"]
            prior = records.get(memory_id)
            if prior is not None and prior.get("revision") != record.get("revision"):
                # 两次短读取间发生修订时，不把不同版本的排名累加成同一候选。
                continue
            records.setdefault(memory_id, record)
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (60 + rank)
            sources.setdefault(memory_id, []).append(label)
    ordered = sorted(records, key=lambda memory_id: (
        not (memory_id == query or records[memory_id].get("name") == query),
        -scores[memory_id], memory_id,
    ))
    for memory_id in ordered[:limit]:
        record = {key: value for key, value in records[memory_id].items()
                  if key not in {"vector", "dimensions", "encoder_id", "score"}}
        record.update(retrieval_score=scores[memory_id], retrieval_sources=sources[memory_id])
        result["items"].append(record)
    return result


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
