"""txtai search backend with Protocol + Registry (Issue #2663).

Provides:
- SearchBackendProtocol: pluggable backend contract
- TxtaiBackend: adapter wrapping txtai Embeddings for hybrid BM25+dense search
- Backend registry: dict-based factory for creating backends by name

All documents are stamped with zone_id metadata. Searches enforce
``WHERE zone_id = :zone_id`` via txtai SQL syntax for namespace isolation.

Graph methods provide semantic graph search using txtai's built-in graph module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol, cast, runtime_checkable

from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)


# =============================================================================
# Protocol
# =============================================================================


@runtime_checkable
class SearchBackendProtocol(Protocol):
    """Backend contract for pluggable search engines."""

    async def index(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Index a batch of documents (full rebuild).

        Each document dict must contain: ``id``, ``text``, ``path``, ``zone_id``.
        """
        ...

    async def upsert(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Upsert documents (insert-or-update)."""
        ...

    async def delete(self, ids: list[str], *, zone_id: str) -> int:
        """Delete documents by id."""
        ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        zone_id: str,
        search_type: str = "hybrid",
        path_filter: str | None = None,
    ) -> list[BaseSearchResult]:
        """Search for documents matching *query* within *zone_id*."""
        ...

    async def startup(self) -> None:
        """Initialize resources (connections, model loading, etc.)."""
        ...

    async def shutdown(self) -> None:
        """Release resources."""
        ...


# =============================================================================
# txtai Backend
# =============================================================================


def _escape_sql_string(value: str) -> str:
    """Escape single-quotes for txtai SQL syntax.

    Applies strict sanitisation: strips control characters and limits
    length to prevent abuse via oversized payloads.
    """
    # Strip ASCII control characters (0x00–0x1F, 0x7F)
    sanitised = "".join(ch for ch in value if ch.isprintable())
    # Truncate to reasonable maximum (search queries / filters)
    sanitised = sanitised[:4096]
    return sanitised.replace("'", "''")


class TxtaiBackend:
    """Search backend wrapping txtai ``Embeddings`` for hybrid BM25+dense search.

    Uses pgvector as storage backend with namespace (zone_id) isolation.
    """

    def __init__(
        self,
        *,
        database_url: str | None = None,
        model: str = "sentence-transformers/all-MiniLM-L6-v2",
        hybrid: bool = True,
        graph: bool = True,
        reranker_model: str | None = None,
        sparse: bool | str = False,
    ) -> None:
        self._database_url = database_url
        self._model = model
        self._hybrid = hybrid
        self._graph = graph
        self._reranker_model = reranker_model
        self._sparse = sparse
        self._embeddings: Any = None
        self._reranker: Any = None
        self.last_rerank_ms: float = 0.0

    async def startup(self) -> None:
        """Initialize txtai Embeddings with pgvector backend (with fallback).

        Fallback chain:
        1. Full hybrid (BM25 + dense embeddings) with pgvector storage
        2. Full hybrid with in-memory storage (pgvector unavailable)
        3. Keyword-only BM25 (embedding model fails to load)
        4. Degraded mode — _embeddings stays None, all searches return []
        """
        from txtai import Embeddings

        # Auto-detect GPU: MPS (Apple Silicon) > CUDA > CPU
        gpu_device: str | bool = False
        try:
            import torch

            if torch.cuda.is_available():
                gpu_device = True  # txtai default CUDA
                logger.info("GPU detected: CUDA")
            elif torch.backends.mps.is_available():
                gpu_device = "mps"
                logger.info("GPU detected: MPS (Apple Silicon)")
        except Exception:
            pass

        config: dict[str, Any] = {
            "path": self._model,
            "content": True,
            "hybrid": self._hybrid,
            "objects": True,
        }

        if gpu_device:
            config["gpu"] = gpu_device

        # Enable SPLADE learned sparse retrieval
        if self._sparse:
            config["sparse"] = self._sparse

        use_pgvector = False
        if self._database_url:
            # Try pgvector backend — fall back to default if not available.
            # txtai >=9.x dispatches in ANNFactory.create() directly and
            # no longer exposes _BACKENDS, so probe the actual module.
            try:
                from txtai.ann.dense.pgvector import PGVector  # noqa: F401

                _has_pgvector = True
            except (ImportError, ModuleNotFoundError):
                _has_pgvector = False

            if _has_pgvector:
                config["backend"] = "pgvector"
                config["database"] = self._database_url
                use_pgvector = True
            else:
                logger.warning(
                    "pgvector backend not available (install txtai[ann]). "
                    "Falling back to default in-memory backend."
                )

        if self._graph:
            config["graph"] = {"backend": "networkx"}

        # Attempt full hybrid init; fall back to keyword-only on failure
        try:
            self._embeddings = Embeddings(config)
        except Exception:
            logger.warning(
                "Full hybrid init failed (model=%s). Falling back to keyword-only (BM25).",
                self._model,
                exc_info=True,
            )
            try:
                bm25_config: dict[str, Any] = {
                    "keyword": True,
                    "content": True,
                    "objects": True,
                }
                self._embeddings = Embeddings(bm25_config)
                self._hybrid = False
                logger.info("Keyword-only (BM25) backend started successfully")
            except Exception:
                logger.error(
                    "BM25 fallback also failed. "
                    "Search daemon will start in degraded mode (no results).",
                    exc_info=True,
                )
                self._embeddings = None

        # Initialize cross-encoder reranker if configured
        if self._reranker_model and self._embeddings is not None:
            try:
                from txtai.pipeline import Similarity

                self._reranker = await asyncio.to_thread(
                    lambda: Similarity(path=self._reranker_model, crossencode=True)
                )
                logger.info("Reranker initialized: %s", self._reranker_model)
            except Exception:
                logger.warning(
                    "Reranker init failed (model=%s). Continuing without reranking.",
                    self._reranker_model,
                    exc_info=True,
                )

        logger.info(
            "txtai backend started: model=%s, hybrid=%s, graph=%s, pgvector=%s, "
            "reranker=%s, sparse=%s, degraded=%s",
            self._model,
            self._hybrid,
            self._graph,
            use_pgvector,
            self._reranker_model if self._reranker else None,
            bool(self._sparse),
            self._embeddings is None,
        )

    async def shutdown(self) -> None:
        """Release txtai resources."""
        self._reranker = None
        if self._embeddings is not None:
            await asyncio.to_thread(self._embeddings.close)
            self._embeddings = None
        logger.info("txtai backend shut down")

    # ----- Index operations ---------------------------------------------------

    async def index(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Index documents (full rebuild for zone_id)."""
        if not self._embeddings or not documents:
            return 0

        stamped = _stamp_zone_id(documents, zone_id)
        rows = [(doc["id"], doc, None) for doc in stamped]
        await asyncio.to_thread(self._embeddings.index, rows)
        return len(rows)

    async def upsert(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Upsert documents (insert-or-update).

        Falls back to ``index()`` on first call when the ANN backend
        hasn't been initialized yet.
        """
        if not self._embeddings or not documents:
            return 0

        stamped = _stamp_zone_id(documents, zone_id)
        rows = [(doc["id"], doc, None) for doc in stamped]

        # txtai requires index() for the first batch to initialize the ANN.
        # After that, upsert() works for incremental updates.
        if getattr(self._embeddings, "ann", None) is None:
            await asyncio.to_thread(self._embeddings.index, rows)
        else:
            await asyncio.to_thread(self._embeddings.upsert, rows)
        return len(rows)

    async def delete(self, ids: list[str], *, zone_id: str) -> int:  # noqa: ARG002
        """Delete documents by id."""
        if not self._embeddings or not ids:
            return 0

        await asyncio.to_thread(self._embeddings.delete, ids)
        return len(ids)

    # ----- Search -------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        zone_id: str,
        search_type: str = "hybrid",
        path_filter: str | None = None,
    ) -> list[BaseSearchResult]:
        """Search with mandatory zone_id isolation via txtai SQL WHERE clause."""
        if not self._embeddings:
            return []

        self.last_rerank_ms = 0.0

        # Over-fetch when reranker is available so it has enough candidates.
        # 2x balances rerank quality vs CPU latency (~10ms per candidate).
        fetch_limit = limit * 2 if self._reranker else limit

        sql = _build_search_sql(query, zone_id=zone_id, path_filter=path_filter, limit=fetch_limit)
        raw: list[dict[str, Any]] = await asyncio.to_thread(self._embeddings.search, sql)

        results: list[BaseSearchResult] = []
        for r in raw:
            score = float(r.get("score", 0.0))
            result = BaseSearchResult(
                path=r.get("path", ""),
                chunk_text=r.get("text", ""),
                score=score,
            )
            if search_type == "keyword":
                result.keyword_score = score
            elif search_type == "semantic":
                result.vector_score = score
            else:  # hybrid
                result.keyword_score = score
                result.vector_score = score
            results.append(result)

        # Cross-encoder reranking
        if self._reranker and results:
            results = await self._rerank_results(query, results, limit)
        else:
            results = results[:limit]

        return results

    async def _rerank_results(
        self,
        query: str,
        results: list[BaseSearchResult],
        limit: int,
    ) -> list[BaseSearchResult]:
        """Rerank results using cross-encoder model."""
        start = time.perf_counter()

        texts = [r.chunk_text for r in results if r.chunk_text]
        if not texts:
            return results[:limit]

        # txtai Similarity returns [(index, score), ...] sorted by score desc
        scored: list[tuple[int, float]] = await asyncio.to_thread(self._reranker, query, texts)

        reranked: list[BaseSearchResult] = []
        for idx, score in scored:
            if idx < len(results):
                result = results[idx]
                result.reranker_score = float(score)
                reranked.append(result)

        self.last_rerank_ms = (time.perf_counter() - start) * 1000
        logger.debug("Reranked %d results in %.1fms", len(reranked), self.last_rerank_ms)
        return reranked[:limit]

    # ----- Graph search -------------------------------------------------------

    async def graph_search(
        self,
        query: str,
        *,
        zone_id: str,
        hops: int = 2,  # noqa: ARG002
        limit: int = 10,
    ) -> list[BaseSearchResult]:
        """Graph-augmented search using txtai's semantic graph."""
        if not self._embeddings or not getattr(self._embeddings, "graph", None):
            return []

        raw = await asyncio.to_thread(self._embeddings.graph.search, query, limit=limit)

        results: list[BaseSearchResult] = []
        for r in raw:
            row = r if isinstance(r, dict) else {"text": str(r), "score": 0.0}
            if row.get("zone_id") != zone_id:
                continue
            results.append(
                BaseSearchResult(
                    path=row.get("path", ""),
                    chunk_text=row.get("text", ""),
                    score=float(row.get("score", 0.0)),
                )
            )
        return results

    async def get_entity_neighbors(
        self,
        entity_id: str,
        *,
        zone_id: str,
        hops: int = 2,
    ) -> list[dict[str, Any]]:
        """N-hop entity traversal via txtai graph.

        Returns list of neighbor dicts with ``id``, ``text``, ``score`` keys.
        """
        if not self._embeddings or not getattr(self._embeddings, "graph", None):
            return []

        graph = self._embeddings.graph
        try:
            # txtai's graph exposes a networkx-compatible interface
            import networkx as nx

            g = graph.backend if hasattr(graph, "backend") else graph
            if not isinstance(g, nx.Graph):
                return []

            if entity_id not in g:
                return []

            neighbors: set[str] = set()
            current_layer = {entity_id}
            for _ in range(hops):
                next_layer: set[str] = set()
                for node in current_layer:
                    for nbr in g.neighbors(node):
                        if nbr not in neighbors and nbr != entity_id:
                            next_layer.add(nbr)
                neighbors.update(next_layer)
                current_layer = next_layer

            results: list[dict[str, Any]] = []
            for nid in neighbors:
                data = g.nodes.get(nid, {})
                if data.get("zone_id") != zone_id:
                    continue
                results.append(
                    {
                        "id": nid,
                        "text": data.get("text", ""),
                        "score": float(data.get("score", 0.0)),
                    }
                )
            return results

        except ImportError:
            return []


# =============================================================================
# Helpers
# =============================================================================


def _build_search_sql(
    query: str,
    *,
    zone_id: str,
    path_filter: str | None = None,
    limit: int = 10,
) -> str:
    """Build a txtai SQL search query with proper escaping.

    Centralises query construction to avoid scattered f-string concatenation
    and ensure consistent sanitisation of user-supplied values.
    """
    clauses = [
        f"similar('{_escape_sql_string(query)}')",
        f"zone_id = '{_escape_sql_string(zone_id)}'",
    ]
    if path_filter:
        clauses.append(f"path LIKE '{_escape_sql_string(path_filter)}%'")

    where = " AND ".join(clauses)
    safe_limit = max(1, min(int(limit), 1000))
    return f"SELECT id, text, score, path, zone_id FROM txtai WHERE {where} LIMIT {safe_limit}"


def _stamp_zone_id(documents: list[dict[str, Any]], zone_id: str) -> list[dict[str, Any]]:
    """Return new document list with zone_id stamped on each (immutable)."""
    return [{**doc, "zone_id": zone_id} for doc in documents]


# =============================================================================
# Backend Registry (Decision #2, #8)
# =============================================================================


SEARCH_BACKENDS: dict[str, type] = {
    "txtai": TxtaiBackend,
}


def create_backend(name: str, **kwargs: Any) -> SearchBackendProtocol:
    """Create a search backend by registry name.

    Args:
        name: Backend name (e.g. "txtai")
        **kwargs: Forwarded to backend constructor

    Returns:
        An instance satisfying :class:`SearchBackendProtocol`

    Raises:
        ValueError: If *name* is not registered
    """
    factory = SEARCH_BACKENDS.get(name)
    if factory is None:
        available = ", ".join(sorted(SEARCH_BACKENDS))
        msg = f"Unknown search backend: {name!r}. Available: [{available}]"
        raise ValueError(msg)
    backend: Any = factory(**kwargs)
    return cast(SearchBackendProtocol, backend)
