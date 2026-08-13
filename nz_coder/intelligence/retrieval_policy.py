"""Deterministic, bounded routing for repository retrieval."""
from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
import re
import time

from nz_coder.runtime.task_policy import detect_task_mode, estimate_text_complexity


RETRIEVAL_STRATEGIES = frozenset({
    "tool-only", "guidance", "auto-context", "policy",
})


@dataclass(frozen=True)
class RepoRoutingSignal:
    repo_size: int
    languages: tuple[str, ...]
    changed_file_count: int
    candidate_modules: tuple[str, ...]
    candidate_files: tuple[str, ...]
    routing_confidence: float
    evidence_confidence: float
    candidate_count: int
    fallback_state: str
    index_status: str
    task_class: str
    recommended_operation: str
    recommended_tools: tuple[str, ...]

    @property
    def retrieval_confidence(self) -> float:
        """Compatibility view; new traces must use the split fields."""
        return self.evidence_confidence


@dataclass(frozen=True)
class RetrievalDecision:
    strategy: str
    signal: RepoRoutingSignal
    guidance: str = ""
    auto_context: str = ""
    fallback: str = ""
    elapsed_ms: float = 0.0

    @property
    def prompt_block(self) -> str:
        parts = [part for part in (self.guidance, self.auto_context) if part]
        if not parts:
            return ""
        return "<repo-routing>\n" + "\n".join(parts) + "\n</repo-routing>"


class RepoRetrievalPolicy:
    """Choose cheap retrieval paths and optionally inject only strong evidence."""

    def __init__(
        self, *, hot_path_ms: float = 100.0, token_budget: int = 500,
        confidence_threshold: float = 0.72, limit: int = 3,
    ) -> None:
        self.hot_path_ms = max(10.0, float(hot_path_ms))
        self.token_budget = max(100, int(token_budget))
        self.confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))
        self.limit = max(1, min(10, int(limit)))
        self._cache: dict[tuple, RetrievalDecision] = {}

    def decide(
        self, query: str, *, service, strategy: str = "guidance",
        changed_paths: tuple[str, ...] = (), semantic_available: bool = False,
    ) -> RetrievalDecision:
        selected = str(strategy or "guidance").casefold()
        if selected not in RETRIEVAL_STRATEGIES:
            raise ValueError(
                "repo retrieval strategy must be tool-only, guidance, auto-context, or policy"
            )
        state = service.state
        cache_key = (
            selected, int(state.generation), str(query), tuple(changed_paths),
            bool(semantic_available),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        started = time.perf_counter()
        task_class, operation, tools, route_confidence = self._route(
            query, changed_paths=changed_paths, semantic_available=semantic_available,
        )
        items: list[dict] = []
        fallback = ""
        wants_auto = selected in {"auto-context", "policy"}
        if wants_auto and operation in {"lookup", "semantic_search", "changed_scope"}:
            try:
                if operation == "semantic_search" and semantic_available:
                    # semantic_search may spend one wait window on index warmup
                    # and another on embedding.  Keep both inside the outer
                    # hot-path deadline while allowing budgets above 100ms to
                    # provide materially more evidence than the 50ms tier.
                    query_wait_ms = max(1.0, self.hot_path_ms * 0.45)
                    future = service.submit_bounded_query(
                        lambda: service.semantic_search(
                            query, limit=self.limit,
                            wait_budget_ms=query_wait_ms,
                        )
                    )
                elif operation == "changed_scope":
                    future = service.submit_bounded_query(
                        lambda: service.changed_scope(
                            changed_paths=list(changed_paths) or None,
                            limit=self.limit, node_limit=20, wait_budget_ms=0,
                        )
                    )
                else:
                    future = service.submit_bounded_query(
                        lambda: service.intent_lookup(
                            query, limit=self.limit, wait_budget_ms=0,
                        )
                    )
                payload = future.result(timeout=self.hot_path_ms / 1000)
                items = self._candidate_items(payload, operation)
                if payload.get("fallback"):
                    fallback = str(payload.get("freshness") or state.status)
            except FutureTimeout:
                fallback = "hot-path-timeout"
            except (RuntimeError, ValueError, OSError) as exc:
                fallback = f"{type(exc).__name__}: {exc}"
        if operation == "semantic_search":
            accepted, item_confidence = self._semantic_candidates(
                items, confidence_threshold=self.confidence_threshold,
            )
        else:
            accepted = [
                item for item in items
                if float(item.get("score") or 0.0) >= self.confidence_threshold
            ]
            item_confidence = max(
                (float(item.get("score") or 0.0) for item in accepted), default=0.0,
            )
        candidate_files = tuple(dict.fromkeys(
            str(item.get("file") or str(item.get("locator") or "").split(":", 1)[0])
            for item in accepted if item.get("file") or item.get("locator")
        ))
        candidate_modules = tuple(dict.fromkeys(
            str(item.get("module_id") or item.get("identity") or "")
            for item in accepted if item.get("kind") == "module" or item.get("module_id")
        ))
        signal = RepoRoutingSignal(
            repo_size=int(state.files_indexed),
            languages=self._languages(service),
            changed_file_count=len(changed_paths),
            candidate_modules=candidate_modules,
            candidate_files=candidate_files,
            routing_confidence=route_confidence,
            evidence_confidence=item_confidence,
            candidate_count=len(accepted),
            fallback_state=fallback,
            index_status=str(state.status), task_class=task_class,
            recommended_operation=operation, recommended_tools=tools,
        )
        guidance = self._guidance(semantic_available) if selected in {"guidance", "policy"} else ""
        auto_context = self._format_auto_context(accepted, operation)
        decision = RetrievalDecision(
            strategy=selected, signal=signal, guidance=guidance,
            auto_context=auto_context, fallback=fallback,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        self._cache = {cache_key: decision}
        return decision

    @staticmethod
    def _route(
        query: str, *, changed_paths: tuple[str, ...], semantic_available: bool,
    ) -> tuple[str, str, tuple[str, ...], float]:
        text = str(query or "")
        mode = detect_task_mode(text)
        complexity = estimate_text_complexity(text)
        paths = re.findall(
            r"(?:^|\s)([\w./-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb))\b", text,
        )
        quoted_hints = re.findall(r"`([^`\n]{2,100})`", text)
        symbol_hints = [
            value for value in quoted_hints
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*",
                value,
            )
        ]
        literal_hints = [value for value in quoted_hints if value not in symbol_hints]
        structural_terms = (
            "call chain", "call path", "caller", "callee", "impact", "dependency",
            "entrypoint", "module", "调用链", "调用路径", "影响", "依赖", "入口", "模块",
        )
        natural_tokens = re.findall(r"[A-Za-z\u4e00-\u9fff]+", text)
        code_tokens = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text)
        identifier_density = sum("_" in token or any(ch.isupper() for ch in token[1:]) for token in code_tokens)
        changed_intent = any(term in text.casefold() for term in (
            "current changes", "changed code", "edited code", "current diff",
            "working tree", "当前改动", "已修改", "变更范围",
        ))
        if paths:
            return "known-location", "read", ("read_file", "grep_search"), 0.95
        if changed_paths and (changed_intent or mode in {"refactor", "bugfix", "discuss"}):
            return "changed-code", "changed_scope", ("repo_context", "read_file"), 0.88
        if literal_hints:
            return "exact-literal", "grep", ("grep_search", "read_file"), 0.94
        if symbol_hints:
            return "known-symbol", "symbol_context", (
                "repo_context", "read_symbol", "find_symbol_callers",
            ), 0.9
        if any(term in text.casefold() for term in structural_terms):
            return "structural", "lookup", ("repo_context", "grep_search", "read_file"), 0.84
        unknown_location = not paths and (
            complexity in {"moderate", "complex"}
            or mode in {"bugfix", "refactor", "feature", "discuss"}
            or len(natural_tokens) >= 9
        )
        # Short natural-language bug reports are common.  They are not exact
        # symbol queries merely because they contain a few identifier-shaped
        # words ("duplicate invoice retries"), so let an available semantic
        # backend participate without weakening the exact path/symbol gates.
        short_business_intent = (
            not paths and not symbol_hints and not any(
                term in text.casefold() for term in structural_terms
            )
            and len(natural_tokens) >= 4
            and identifier_density == 0
            and mode in {"bugfix", "feature", "discuss", "unknown"}
        )
        vocabulary_mismatch = (
            unknown_location
            and identifier_density == 0
        )
        if (vocabulary_mismatch or short_business_intent) and semantic_available:
            return "business-intent", "semantic_search", (
                "semantic_search", "repo_context", "read_file",
            ), 0.82
        if unknown_location or mode in {"bugfix", "refactor", "discuss"}:
            return "unknown-location", "lookup", (
                "repo_context", "grep_search", "read_file",
            ), 0.76
        return "simple", "grep", ("grep_search", "read_file"), 0.7

    @staticmethod
    def _candidate_items(payload: dict, operation: str) -> list[dict]:
        if operation == "semantic_search":
            return [dict(item) for item in payload.get("items", ())]
        if operation == "lookup":
            return [dict(item) for item in payload.get("items", ())]
        result = []
        for path in payload.get("changed_files", ()):
            result.append({
                "kind": "file", "locator": str(path), "score": 1.0,
                "confidence": 1.0, "source": "changed-scope",
            })
        return result

    @staticmethod
    def _semantic_candidates(
        items: list[dict], *, confidence_threshold: float,
    ) -> tuple[list[dict], float]:
        """Calibrate provider-specific cosine scores using rank separation.

        Absolute cosine values are not comparable between embedding models.
        Auto-context therefore accepts only one leading candidate and only
        when it is separated from the next distinct locator.  Structural
        context can expand from that identity after localization.
        """
        distinct: list[dict] = []
        seen: set[str] = set()
        for raw in items:
            item = dict(raw)
            locator = str(item.get("file") or item.get("locator") or "")
            key = locator or str(item.get("symbol_id") or item.get("identity") or "")
            if key in seen:
                continue
            seen.add(key)
            distinct.append(item)
        if not distinct:
            return [], 0.0
        top_score = float(distinct[0].get("score") or 0.0)
        if len(distinct) == 1:
            return (
                ([distinct[0]], top_score)
                if top_score >= confidence_threshold else ([], 0.0)
            )
        runner_up = float(distinct[1].get("score") or 0.0)
        margin = top_score - runner_up
        if top_score < 0.10 or margin < 0.015:
            return [], 0.0
        evidence_confidence = min(1.0, 0.65 + margin * 4.0)
        if evidence_confidence < confidence_threshold:
            return [], 0.0
        return [distinct[0]], evidence_confidence

    @staticmethod
    def _languages(service) -> tuple[str, ...]:
        try:
            return tuple(sorted({
                str(entry.language) for entry in service.index.snapshot().files
                if entry.language
            }))
        except (OSError, RuntimeError):
            return ()

    @staticmethod
    def _guidance(semantic_available: bool) -> str:
        semantic = (
            " Business-language intent that may not match code vocabulary: use semantic_search, "
            "then follow its symbol_id/module_id with repo_context."
            if semantic_available else
            " Business-language intent with vocabulary mismatch: start broad with repo_context lookup or grep variants."
        )
        return (
            "Retrieval routing: exact literal -> grep_search; exact symbol -> repo_context "
            "symbol_context or LSP; unknown structural location -> repo_context lookup; "
            "changed-code reasoning -> changed_scope/impact." + semantic
        )

    def _format_auto_context(self, items: list[dict], operation: str) -> str:
        if not items:
            return ""
        lines = [f"High-confidence bounded {operation} candidates:"]
        for item in items[:self.limit]:
            identity = item.get("symbol_id") or item.get("identity") or ""
            locator = item.get("file") or item.get("locator") or ""
            title = item.get("title") or identity or locator
            lines.append(
                f"- {title} | {locator} | identity={identity} | score={float(item.get('score') or 0):.3f}"
            )
        rendered = "\n".join(lines)
        max_chars = self.token_budget * 4
        return rendered[:max_chars]


__all__ = [
    "RETRIEVAL_STRATEGIES", "RepoRetrievalPolicy", "RepoRoutingSignal",
    "RetrievalDecision",
]
