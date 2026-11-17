"""Deterministic, bounded routing for repository retrieval."""
from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import time


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
        known_paths: tuple[str, ...] = (), task_context: tuple = (),
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
            task_context,
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
        # 自动检索只消费模型文本中明确给出的定位符，不根据自然语言猜检索路线。
        if wants_auto and operation == "symbol_context":
            symbol = self._quoted_hints(query)[0][0]
            try:
                future = service.submit_bounded_query(
                    lambda: service.intent_lookup(
                        symbol, kind="symbol", limit=self.limit, wait_budget_ms=0,
                    )
                )
                payload = future.result(timeout=self.hot_path_ms / 1000)
                items = [dict(item) for item in payload.get("items", ())]
                if payload.get("fallback"):
                    fallback = str(payload.get("freshness") or state.status)
            except FutureTimeout:
                fallback = "hot-path-timeout"
            except (RuntimeError, ValueError, OSError) as exc:
                fallback = f"{type(exc).__name__}: {exc}"
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
        paths = re.findall(
            r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb))\b", text,
        )
        symbol_hints, literal_hints = RepoRetrievalPolicy._quoted_hints(text)
        if paths:
            return "known-location", "read", ("read_file", "grep_search"), 0.95
        if known_paths:
            return "known-location", "read", ("read_file",), 0.97
        if literal_hints:
            return "exact-literal", "grep", ("grep_search", "read_file"), 0.94
        if symbol_hints:
            return "known-symbol", "symbol_context", (
                "repo_context", "read_symbol", "find_symbol_callers",
            ), 0.9
        tools = ("repo_context", "grep_search", "read_file")
        if semantic_available:
            tools = ("semantic_search", *tools)
        return "unknown-location", "lookup", tools, 0.0

    @staticmethod
    def _quoted_hints(query: str) -> tuple[list[str], list[str]]:
        quoted = re.findall(r"`([^`\n]{2,100})`", str(query or ""))
        symbols = [
            value for value in quoted
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*",
                value,
            )
        ]
        return symbols, [value for value in quoted if value not in symbols]

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
            " Available retrieval tools also include semantic_search; choose it when useful."
            if semantic_available else ""
        )
        return (
            "Retrieval routing: exact literal -> grep_search; exact symbol -> repo_context "
            "symbol_context or LSP; otherwise choose general discovery with repo_context lookup "
            "or grep_search. Changed paths are available to changed_scope/impact."
            + semantic
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
