"""Deterministic, bounded routing for repository retrieval."""
from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import time

from nz_coder.runtime.agent.task_policy import detect_task_mode, estimate_text_complexity


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
        known_paths: tuple[str, ...] = (),
    ) -> RetrievalDecision:
        selected = str(strategy or "guidance").casefold()
        if selected not in RETRIEVAL_STRATEGIES:
            raise ValueError(
                "repo retrieval strategy must be tool-only, guidance, auto-context, or policy"
            )
        declared_paths = self._normalize_known_paths(known_paths)
        state = service.state
        cache_key = (
            selected, int(state.generation), str(state.status), str(state.error),
            str(query), tuple(changed_paths), bool(semantic_available), declared_paths,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        started = time.perf_counter()
        task_class, operation, tools, route_confidence = self._route(
            query,
            changed_paths=changed_paths,
            semantic_available=semantic_available,
            known_paths=declared_paths,
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
        candidate_files = tuple(dict.fromkeys((
            *declared_paths,
            *(
                str(item.get("file") or str(item.get("locator") or "").split(":", 1)[0])
                for item in accepted if item.get("file") or item.get("locator")
            ),
        )))
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
            candidate_count=len(candidate_files),
            fallback_state=fallback,
            index_status=str(state.status), task_class=task_class,
            recommended_operation=operation, recommended_tools=tools,
        )
        guidance = (
            self._guidance(
                semantic_available,
                task_class=task_class,
                known_paths=declared_paths,
            )
            if selected in {"guidance", "policy"} else ""
        )
        auto_context = self._format_auto_context(
            accepted,
            operation,
            service=service,
            state=state,
            changed_paths=changed_paths,
        )
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
        known_paths: tuple[str, ...] = (),
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
        if known_paths:
            return "known-location", "read", ("read_file",), 0.97
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
        return tuple(getattr(service.state, "languages", ()) or ())

    @staticmethod
    def _normalize_known_paths(values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for raw in values:
            value = str(raw or "").strip().replace("\\", "/")
            path = PurePosixPath(value)
            if not value or path.is_absolute() or ".." in path.parts:
                continue
            normalized = path.as_posix()
            if normalized not in result:
                result.append(normalized)
        return tuple(result[:12])

    @staticmethod
    def _guidance(
        semantic_available: bool,
        *,
        task_class: str = "",
        known_paths: tuple[str, ...] = (),
    ) -> str:
        if task_class == "known-location" and known_paths:
            return (
                "Declared target paths already resolve the initial workset: "
                + ", ".join(known_paths)
                + ". Inspect only the needed targets with read_file; skip broad repository "
                "orientation unless a declared path is missing."
            )
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

    def _format_auto_context(
        self,
        items: list[dict],
        operation: str,
        *,
        service=None,
        state=None,
        changed_paths: tuple[str, ...] = (),
    ) -> str:
        if not items:
            return ""
        state_status = str(getattr(state, "status", "unknown") or "unknown")
        state_generation = getattr(state, "generation", None)
        lines = [
            f"High-confidence bounded {operation} candidates "
            f"(status={state_status}, generation={state_generation}):"
        ]
        scope = None
        if (
            operation == "changed_scope"
            and service is not None
            and str(getattr(state, "status", "")) == "ready"
            and changed_paths
        ):
            try:
                scope = service.changed_scope(
                    changed_paths=list(changed_paths),
                    limit=self.limit,
                    node_limit=20,
                    wait_budget_ms=0,
                )
            except (RuntimeError, ValueError, OSError):
                scope = None

        for item in items[:self.limit]:
            identity = item.get("symbol_id") or item.get("identity") or ""
            locator = item.get("file") or item.get("locator") or ""
            title = item.get("title") or identity or locator
            score = float(item.get("score") or 0)
            confidence = float(item.get("confidence") or score)
            freshness = str(
                item.get("freshness")
                or ("indexed" if state_status == "ready" else state_status)
            )
            lines.append(
                f"- {title} | {locator} | identity={identity} | score={score:.3f} "
                f"| confidence={confidence:.3f} | freshness={freshness} "
                f"| source={item.get('source') or 'unknown'}"
            )
            snippet = self._clean_evidence_value(item.get("snippet"))
            if snippet:
                lines.append(f"  Summary: {snippet}")

            detail = scope if operation == "changed_scope" else None
            if detail is None and service is not None and str(identity):
                try:
                    if str(identity).startswith("symbol:"):
                        detail = service.symbol_context(str(identity), limit=8, wait_budget_ms=0)
                    elif str(identity).startswith("module:"):
                        detail = service.module_context(str(identity), wait_budget_ms=0)
                except (RuntimeError, ValueError, OSError):
                    detail = None
            lines.extend(self._render_candidate_evidence(
                detail,
                locator=str(locator),
                freshness=freshness,
            ))
        max_chars = self.token_budget * 4
        rendered_lines: list[str] = []
        size = 0
        for line in lines:
            addition = len(line) + (1 if rendered_lines else 0)
            if rendered_lines and size + addition > max_chars:
                break
            rendered_lines.append(line)
            size += addition
        return "\n".join(rendered_lines)

    @classmethod
    def _render_candidate_evidence(
        cls,
        detail: dict | None,
        *,
        locator: str,
        freshness: str,
    ) -> list[str]:
        """Render a few traceable facts without copying a full graph payload."""
        if not isinstance(detail, dict):
            return [f"  Evidence: unavailable ({freshness}); use repo_context or grep_search."]

        facts: list[str] = []
        definition = detail.get("definition")
        if isinstance(definition, dict):
            path = cls._clean_evidence_value(
                definition.get("path") or definition.get("file_path")
            )
            line = definition.get("line")
            if path:
                facts.append(f"definition={path}:{line}" if line else f"definition={path}")
            module = cls._clean_evidence_value(definition.get("module_id"))
            if module:
                facts.append(f"module={module}")
        signature = cls._clean_evidence_value(detail.get("signature"))
        if signature:
            facts.append(f"signature={signature}")
        for key, label in (
            ("dependencies", "dependencies"),
            ("dependents", "dependents"),
            ("related_tests", "related_tests"),
            ("entry_files", "entry_files"),
            ("direct_callers", "callers"),
            ("impacted_callers", "impacted_callers"),
            ("changed_symbols", "changed_symbols"),
            ("dependent_modules", "dependents"),
            ("risk", "risk"),
        ):
            value = detail.get(key)
            rendered = cls._clean_evidence_list(value)
            if rendered:
                facts.append(f"{label}={rendered}")
        callers = cls._clean_evidence_list(
            [item.get("caller") for item in detail.get("callers", ()) if isinstance(item, dict)]
        )
        callees = cls._clean_evidence_list(
            [item.get("callee") for item in detail.get("callees", ()) if isinstance(item, dict)]
        )
        if callers:
            facts.append(f"callers={callers}")
        if callees:
            facts.append(f"callees={callees}")
        alternatives = cls._clean_evidence_list(
            [
                f"{item.get('path') or item.get('file_path')}:{item.get('line')}"
                for item in detail.get("alternatives", ())
                if isinstance(item, dict)
            ]
        )
        if alternatives:
            facts.append(f"alternatives={alternatives}")
        if detail.get("warnings"):
            facts.append("warnings=" + cls._clean_evidence_list(detail["warnings"]))
        if facts:
            return ["  Evidence: " + " | ".join(facts)] + [
                f"  Next read: read_file path={locator.split(':', 1)[0]}"
            ]
        return [f"  Evidence: {freshness}; use repo_context or grep_search to verify."]

    @staticmethod
    def _clean_evidence_value(value) -> str:
        text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
        return text[:120]

    @classmethod
    def _clean_evidence_list(cls, values) -> str:
        if isinstance(values, (str, bytes)):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return ""
        cleaned = [cls._clean_evidence_value(value) for value in values]
        cleaned = [value for value in cleaned if value]
        return ", ".join(list(dict.fromkeys(cleaned))[:8])


__all__ = [
    "RETRIEVAL_STRATEGIES", "RepoRetrievalPolicy", "RepoRoutingSignal",
    "RetrievalDecision",
]
