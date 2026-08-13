"""Optional, provider-neutral semantic retrieval over repository identities.

This module is deliberately experimental.  It reuses the structural index for
chunk boundaries and identity binding; it does not create a second parser or a
mandatory vector-store dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import math
from pathlib import Path
from threading import RLock
import time
from typing import Protocol, Sequence

from nz_coder.intelligence.code_index import PersistentCodeIndex


class EmbeddingProvider(Protocol):
    """Smallest provider boundary needed by the local experiment."""

    @property
    def identity(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class SemanticCodeChunk:
    file: str
    start_line: int
    end_line: int
    code_chunk: str
    symbol_id: str | None = None
    module_id: str | None = None
    title: str = ""

    @property
    def embedding_text(self) -> str:
        header = " ".join(
            value for value in (self.title, self.symbol_id or "", self.module_id or "")
            if value
        )
        return f"{header}\n{self.code_chunk}" if header else self.code_chunk


@dataclass(frozen=True)
class SemanticSearchResult:
    file: str
    start_line: int
    end_line: int
    code_chunk: str
    score: float
    symbol_id: str | None
    module_id: str | None
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


class ChunkStore(Protocol):
    """Replace-and-query boundary for a generation-scoped vector store."""

    def replace(
        self, generation: int, chunks: Sequence[SemanticCodeChunk],
        vectors: Sequence[Sequence[float]],
    ) -> None: ...

    def search(
        self, query_vector: Sequence[float], *, path: str | None, limit: int,
    ) -> list[tuple[SemanticCodeChunk, float]]: ...


class SemanticCodeIndex(Protocol):
    """Provider-neutral code retrieval contract consumed by the tool/runtime."""

    @property
    def available(self) -> bool: ...

    def search(
        self, query: str, *, path: str | None = None, limit: int = 10,
    ) -> list[SemanticSearchResult]: ...


class InMemoryChunkStore:
    """Dependency-free cosine store suitable for deciding whether vectors help."""

    def __init__(self) -> None:
        self.generation = 0
        self._items: tuple[tuple[SemanticCodeChunk, tuple[float, ...]], ...] = ()

    def replace(
        self, generation: int, chunks: Sequence[SemanticCodeChunk],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("semantic chunks and vectors must have equal length")
        self.generation = int(generation)
        self._items = tuple(
            (chunk, _normalized(vector)) for chunk, vector in zip(chunks, vectors)
        )

    def search(
        self, query_vector: Sequence[float], *, path: str | None, limit: int,
    ) -> list[tuple[SemanticCodeChunk, float]]:
        query = _normalized(query_vector)
        if not query:
            return []
        prefix = str(path or "").replace("\\", "/").strip("/")
        scored = []
        for chunk, vector in self._items:
            if prefix and not (
                chunk.file == prefix or chunk.file.startswith(prefix + "/")
            ):
                continue
            if len(vector) != len(query):
                continue
            score = sum(left * right for left, right in zip(query, vector))
            scored.append((chunk, max(-1.0, min(1.0, score))))
        scored.sort(key=lambda item: (-item[1], item[0].file, item[0].start_line))
        return scored[:max(1, int(limit))]


class RepositorySemanticIndex:
    """Generation-aware semantic index built from indexed symbols and spans."""

    def __init__(
        self, workspace: Path, index: PersistentCodeIndex,
        provider: EmbeddingProvider, *, store: ChunkStore | None = None,
        max_chunks: int = 10_000, section_lines: int = 80,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.index = index
        self.provider = provider
        self.store = store or InMemoryChunkStore()
        self.max_chunks = max(1, int(max_chunks))
        self.section_lines = max(20, int(section_lines))
        self._generation = -1
        self._lock = RLock()
        self._last_embedded_chunks = 0
        self._total_embedded_chunks = 0
        self._last_build_ms = 0.0

    @property
    def available(self) -> bool:
        available = getattr(self.provider, "available", None)
        if available is not None:
            return bool(available)
        return callable(getattr(self.provider, "embed", None))

    @property
    def generation(self) -> int:
        return self._generation

    def metrics(self) -> dict:
        return {
            "generation": self._generation,
            "last_embedded_chunks": self._last_embedded_chunks,
            "total_embedded_chunks": self._total_embedded_chunks,
            "last_build_ms": round(self._last_build_ms, 3),
            "provider_status": str(getattr(self.provider, "status", "ready")),
            "experimental": True,
        }

    def search(
        self, query: str, *, path: str | None = None, limit: int = 10,
    ) -> list[SemanticSearchResult]:
        if not str(query).strip():
            return []
        self._ensure_generation()
        vector = self.provider.embed([str(query)])[0]
        return [
            SemanticSearchResult(
                file=chunk.file, start_line=chunk.start_line,
                end_line=chunk.end_line, code_chunk=chunk.code_chunk,
                score=round(float(score), 6), symbol_id=chunk.symbol_id,
                module_id=chunk.module_id,
                source=f"embedding:{self.provider.identity}",
            )
            for chunk, score in self.store.search(vector, path=path, limit=limit)
        ]

    def _ensure_generation(self) -> None:
        snapshot = self.index.snapshot()
        if self._generation == snapshot.generation:
            return
        with self._lock:
            snapshot = self.index.snapshot()
            if self._generation == snapshot.generation:
                return
            started = time.perf_counter()
            chunks = self._chunks(snapshot.files)[:self.max_chunks]
            vectors = self.provider.embed([chunk.embedding_text for chunk in chunks])
            self.store.replace(snapshot.generation, chunks, vectors)
            self._generation = snapshot.generation
            self._last_embedded_chunks = len(chunks)
            self._total_embedded_chunks += len(chunks)
            self._last_build_ms = (time.perf_counter() - started) * 1000

    def _chunks(self, files) -> list[SemanticCodeChunk]:
        result: list[SemanticCodeChunk] = []
        for entry in files:
            target = self.workspace / entry.path
            try:
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for symbol in entry.symbols:
                start = max(1, int(symbol.line))
                end = min(len(lines), max(start, int(symbol.end_line or start)))
                code = "\n".join(lines[start - 1:end]).strip()
                if not code:
                    continue
                result.append(SemanticCodeChunk(
                    file=entry.path, start_line=start, end_line=end,
                    code_chunk=code, symbol_id=symbol.symbol_id or None,
                    module_id=symbol.module_id or entry.module_id or None,
                    title=" ".join(filter(None, (
                        symbol.qualified_name, symbol.signature or "", symbol.kind,
                    ))),
                ))
            if result and any(chunk.file == entry.path for chunk in result):
                continue
            for offset in range(0, len(lines), self.section_lines):
                code = "\n".join(lines[offset:offset + self.section_lines]).strip()
                if code:
                    result.append(SemanticCodeChunk(
                        file=entry.path, start_line=offset + 1,
                        end_line=min(len(lines), offset + self.section_lines),
                        code_chunk=code, module_id=entry.module_id or None,
                        title=entry.path,
                    ))
        return result


class SentenceTransformerEmbeddingProvider:
    """Lazy optional adapter; importing NZ-Coder never requires this package."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self.model = str(model)
        self._encoder = None
        self._load_error = ""
        self._status = "configured"
        self._lock = RLock()

    @property
    def identity(self) -> str:
        return f"sentence-transformers/{self.model}"

    @property
    def available(self) -> bool:
        return self.status == "ready"

    @property
    def status(self) -> str:
        """Return a truthful provider lifecycle state.

        ``configured`` means the model was requested but has not been loaded;
        it must not be treated as an exposed/ready semantic capability.
        """
        with self._lock:
            return self._status

    @property
    def load_error(self) -> str:
        return self._load_error

    def prepare(self) -> None:
        """Load the configured model before a run exposes semantic_search."""
        if importlib.util.find_spec("sentence_transformers") is None:
            with self._lock:
                self._status = "unavailable"
                self._load_error = "sentence_transformers is not installed"
            raise RuntimeError(
                "Semantic retrieval unavailable: install the semantic-experiment extra"
            )
        self.embed(["semantic capability probe"])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            if self._encoder is None:
                self._status = "loading"
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    self._load_error = str(exc)
                    self._status = "unavailable"
                    raise RuntimeError(
                        "semantic retrieval requires the semantic-experiment extra"
                    ) from exc
                try:
                    self._encoder = SentenceTransformer(self.model)
                except Exception as exc:
                    self._load_error = f"{type(exc).__name__}: {exc}"
                    self._status = "failed"
                    raise RuntimeError(
                        f"semantic model {self.model!r} could not be loaded: {exc}"
                    ) from exc
                self._status = "ready"
            values = self._encoder.encode(
                list(texts), normalize_embeddings=True, show_progress_bar=False,
            )
        return [list(map(float, vector)) for vector in values]


_PROVIDER_LOCK = RLock()
_PROVIDERS: dict[str, SentenceTransformerEmbeddingProvider] = {}


def sentence_transformer_provider(
    model: str = "sentence-transformers/all-MiniLM-L6-v2",
) -> SentenceTransformerEmbeddingProvider:
    """Share one lazy local encoder across workspace-scoped experiment indexes."""
    identity = str(model)
    with _PROVIDER_LOCK:
        provider = _PROVIDERS.get(identity)
        if provider is None:
            provider = SentenceTransformerEmbeddingProvider(identity)
            _PROVIDERS[identity] = provider
        return provider


def _normalized(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    norm = math.sqrt(sum(value * value for value in values))
    if not values or norm <= 0:
        return ()
    return tuple(value / norm for value in values)


__all__ = [
    "ChunkStore", "EmbeddingProvider", "InMemoryChunkStore",
    "RepositorySemanticIndex", "SemanticCodeChunk", "SemanticCodeIndex",
    "SemanticSearchResult", "SentenceTransformerEmbeddingProvider",
    "sentence_transformer_provider",
]
