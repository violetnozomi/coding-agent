"""Deterministic bounded lexical search over tool definitions."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from nz_coder.tool_platform.catalog import ToolCatalog, ToolDefinition


_TERM = re.compile(r"[A-Za-z0-9_+-]+")


def _tokens(value: str) -> list[str]:
    return [item.lower().strip("+-_") for item in _TERM.findall(value) if item.strip("+-_")]


@dataclass(frozen=True)
class ToolSearchResult:
    name: str
    score: float
    matched_terms: tuple[str, ...]
    definition: ToolDefinition


class ToolSearchIndex:
    """Small BM25-like index supporting exact `select:` and `+required` queries."""

    def __init__(self, catalog: ToolCatalog) -> None:
        self.catalog = catalog
        self._terms: dict[str, dict[str, int]] = {}
        self._documents: dict[str, str] = {}
        for definition in catalog.definitions():
            text = " ".join((
                definition.name.replace("_", " "),
                definition.description,
                str(definition.parameters),
            )).lower()
            counts: dict[str, int] = {}
            for term in _tokens(text):
                counts[term] = counts.get(term, 0) + 1
            self._terms[definition.name] = counts
            self._documents[definition.name] = text

    def search(self, query: str, limit: int = 5) -> tuple[ToolSearchResult, ...]:
        value = str(query or "").strip()
        capped = max(0, min(int(limit), 15))
        if not value or capped == 0:
            return ()
        if value.lower().startswith("select:"):
            names = re.split(r"[,\s]+", value.split(":", 1)[1])
            results = []
            for name in dict.fromkeys(item for item in names if item):
                definition = self.catalog.get(name)
                if definition is not None:
                    results.append(ToolSearchResult(name, 100.0, (name,), definition))
            return tuple(results[:capped])
        raw = _TERM.findall(value)
        required = tuple(item[1:].lower() for item in raw if item.startswith("+") and len(item) > 1)
        loose = tuple(item.lower() for item in raw if not item.startswith("+"))
        total = max(1, len(self._terms))
        scored = []
        for name, counts in self._terms.items():
            document = self._documents[name]
            if any(term not in counts and term not in document for term in required):
                continue
            matched = tuple(
                term for term in (*required, *loose)
                if term in counts or term in document
            )
            if loose and not any(term in matched for term in loose):
                continue
            score = 0.0
            for term in matched:
                frequency = counts.get(term, 1)
                documents = sum(term in item or term in self._documents[key] for key, item in self._terms.items())
                score += frequency * math.log(1.0 + total / max(1, documents))
                if term in name.lower().split("_"):
                    score += 2.0
                elif term in name.lower():
                    score += 1.0
            scored.append(ToolSearchResult(name, score, matched, self.catalog.require(name)))
        scored.sort(key=lambda item: (-item.score, item.name))
        return tuple(scored[:capped])


__all__ = ["ToolSearchIndex", "ToolSearchResult"]
