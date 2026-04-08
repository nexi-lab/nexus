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
import contextlib
import logging
import time
from typing import Any, Protocol, cast, runtime_checkable

from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)

_RERANK_MAX_CHARS = 800
_RERANK_MAX_WORDS = 128


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


def _escape_like_string(value: str) -> str:
    """Escape a value for use in a SQL LIKE clause.

    Extends ``_escape_sql_string`` by also escaping the LIKE wildcard
    characters ``%`` and ``_`` so that the value is matched literally
    (Issue #3062).
    """
    escaped = _escape_sql_string(value)
    return escaped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TxtaiBackend:
    """Search backend wrapping txtai ``Embeddings`` for hybrid BM25+dense search.

    Uses pgvector as storage backend with namespace (zone_id) isolation.
    """

    def __init__(
        self,
        *,
        database_url: str | None = None,
        model: str = "sentence-transformers/all-MiniLM-L6-v2",
        vectors: dict[str, Any] | None = None,
        hybrid: bool = True,
        graph: bool = True,
        reranker_model: str | None = None,
        sparse: bool | str = False,
        embedding_cache: Any | None = None,
        data_path: str | None = None,
    ) -> None:
        self._database_url = database_url
        self._model = model
        self._vectors = dict(vectors or {})
        self._hybrid = hybrid
        self._graph = graph
        self._reranker_model = reranker_model
        self._sparse = sparse
        self._embedding_cache = embedding_cache
        self._embeddings: Any = None
        self._reranker: Any = None
        self.last_rerank_ms: float = 0.0
        self._started = False
        self._startup_lock = asyncio.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._reranker_task: asyncio.Task[None] | None = None
        # Path for txtai config.json — needed for pgvector persistence.
        # txtai stores index metadata (dimensions, offset) in a local file
        # and reads it back on load() to reconnect to pgvector tables.
        self._config_path = data_path or "/app/data/.txtai-index"
        # Serialise all access to _embeddings / _reranker across coroutines.
        # faiss (used by txtai) is NOT thread-safe for concurrent search+write
        # operations. Since asyncio.to_thread() dispatches to a thread pool,
        # concurrent coroutines without this lock cause native segfaults.
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        """Initialize txtai resources once."""
        if self._started or self._embeddings is not None:
            if self._embeddings is not None:
                self._started = True
            return

        async with self._startup_lock:
            if self._started:
                return
            if self._startup_task is None or self._startup_task.done():
                self._startup_task = asyncio.create_task(self._startup_impl())
            task = self._startup_task

        await task

    def kickoff_startup(self) -> None:
        """Begin backend startup in the background without blocking app readiness."""
        if self._started or self._embeddings is not None:
            if self._embeddings is not None:
                self._started = True
            return
        if self._startup_task is None or self._startup_task.done():
            self._startup_task = asyncio.create_task(self._startup_impl())

    @staticmethod
    def _patch_litellm_batching() -> None:
        """Patch txtai's LiteLLM encode to batch API calls.

        txtai's Vectors.index() batches documents (default 500) but passes
        the entire batch to LiteLLM.encode() as a single API call. OpenAI
        limits requests to 300k tokens. This patch splits large batches.
        """
        try:
            import litellm as litellm_api
            import numpy as np
            import txtai.vectors.dense.litellm as litellm_mod

            original_encode = litellm_mod.LiteLLM.encode

            def batched_encode(self_inner: Any, data: list, category: Any = None) -> Any:
                import time as _time

                batch_size = 10  # ~80k tokens max per API call
                if len(data) <= batch_size:
                    return original_encode(self_inner, data, category)

                all_embeddings: list = []
                for i in range(0, len(data), batch_size):
                    batch = data[i : i + batch_size]
                    for attempt in range(5):
                        try:
                            response = litellm_api.embedding(
                                model=self_inner.config.get("path"),
                                input=batch,
                                **self_inner.config.get("vectors", {}),
                            )
                            all_embeddings.extend([x["embedding"] for x in response.data])
                            break
                        except Exception as exc:
                            if "rate" in str(exc).lower() or "429" in str(exc):
                                wait = min(60, 10 * (attempt + 1))
                                logger.warning("Rate limited, waiting %ds", wait)
                                _time.sleep(wait)
                            elif "max" in str(exc).lower() and "token" in str(exc).lower():
                                # Single doc too long — try one by one
                                for doc in batch:
                                    try:
                                        r = litellm_api.embedding(
                                            model=self_inner.config.get("path"),
                                            input=[doc[:30000]],
                                            **self_inner.config.get("vectors", {}),
                                        )
                                        all_embeddings.extend([x["embedding"] for x in r.data])
                                    except Exception:
                                        # Skip doc — use zero vector
                                        all_embeddings.append([0.0] * 1536)
                                break
                            else:
                                raise
                return np.array(all_embeddings, dtype=np.float32)

            litellm_mod.LiteLLM.encode = batched_encode
            logger.info("Patched txtai LiteLLM encode with batching (batch_size=10)")
        except (ImportError, AttributeError) as exc:
            logger.debug("LiteLLM batching patch skipped: %s", exc)

    async def _startup_impl(self) -> None:
        """Initialize txtai Embeddings with pgvector backend (with fallback).

        Fallback chain:
        1. Full hybrid (BM25 + dense embeddings) with pgvector storage
        2. Full hybrid with in-memory storage (pgvector unavailable)
        3. Keyword-only BM25 (embedding model fails to load)
        4. Degraded mode — _embeddings stays None, all searches return []
        """
        self._patch_litellm_batching()

        try:
            from txtai import Embeddings
        except ModuleNotFoundError:
            logger.warning("txtai package not installed; starting in degraded search mode")
            self._embeddings = None
            self._started = True
            return

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

        # txtai treats ``content=True`` as "store content in SQLite". When Nexus
        # has a database URL, keep both ANN vectors and content/object storage on
        # the same client-server database instead of silently creating a local
        # SQLite sidecar that can crash under concurrent indexing/search.
        content_store: bool | str = self._database_url or True
        config: dict[str, Any] = {
            "path": self._model,
            "content": content_store,
            "hybrid": self._hybrid,
            "objects": True,
            # Batch size for API-backed embeddings (OpenAI limit: 300k tokens/request).
            # Some docs are very long (~8k tokens each), so keep batch small.
            # 10 docs × ~8k tokens max = ~80k tokens per API call — safe margin.
            "batch": 10,
        }
        if self._vectors:
            config["vectors"] = dict(self._vectors)

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
                config["pgvector"] = {"url": self._database_url}
                use_pgvector = True
            else:
                logger.warning(
                    "pgvector backend not available (install txtai[ann]). "
                    "Falling back to default in-memory backend."
                )

        if self._graph:
            config["graph"] = {"backend": "networkx"}

        # Try loading existing pgvector index first (survives restarts)
        if use_pgvector:
            try:
                probe = Embeddings()
                if probe.exists(self._config_path):
                    probe.load(self._config_path)
                    self._embeddings = probe
                    logger.info(
                        "Loaded existing txtai index from pgvector (config=%s, count=%d)",
                        self._config_path,
                        probe.count() or 0,
                    )
                else:
                    logger.info(
                        "No existing txtai index at %s — will create on first index()",
                        self._config_path,
                    )
            except Exception:
                logger.debug("Failed to load existing index, creating fresh", exc_info=True)

        # Create fresh Embeddings if we didn't load from pgvector
        if self._embeddings is None:
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
                        "content": content_store,
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

        # Mark backend usable as soon as embeddings are ready. Reranker startup
        # can continue in the background without blocking indexing/search.
        self._started = True

        # Initialize cross-encoder reranker in the background if configured.
        if self._reranker_model and self._embeddings is not None:
            self._reranker_task = asyncio.create_task(self._init_reranker())

        logger.info(
            "txtai backend started: model=%s, hybrid=%s, graph=%s, pgvector=%s, "
            "reranker=%s, sparse=%s, degraded=%s",
            self._model,
            self._hybrid,
            self._graph,
            use_pgvector,
            self._reranker_model,
            bool(self._sparse),
            self._embeddings is None,
        )

    async def _init_reranker(self) -> None:
        """Load the optional cross-encoder reranker without blocking backend readiness."""
        try:
            from txtai.pipeline import Similarity

            reranker = await asyncio.to_thread(
                lambda: Similarity(path=self._reranker_model, crossencode=True)
            )
            async with self._lock:
                self._reranker = reranker
            logger.info("Reranker initialized: %s", self._reranker_model)
        except Exception:
            logger.warning(
                "Reranker init failed (model=%s). Continuing without reranking.",
                self._reranker_model,
                exc_info=True,
            )
            async with self._lock:
                self._reranker = None

    async def shutdown(self) -> None:
        """Release txtai resources."""
        if self._reranker_task is not None and not self._reranker_task.done():
            self._reranker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reranker_task
        self._reranker_task = None
        async with self._lock:
            self._reranker = None
            if self._embeddings is not None:
                await asyncio.to_thread(self._embeddings.close)
                self._embeddings = None
            self._started = False
        logger.info("txtai backend shut down")

    # ----- Index operations ---------------------------------------------------

    def _save(self) -> None:
        """Persist txtai index: commit pgvector transactions + write config.json.

        txtai only commits DB transactions in ``save()``. Without this,
        pgvector transactions stay open indefinitely, causing deadlocks.
        The config.json file stores index metadata (dimensions, offset)
        needed to reload from pgvector on restart.
        """
        if self._embeddings:
            try:
                import os

                os.makedirs(self._config_path, exist_ok=True)
                self._embeddings.save(self._config_path)
            except Exception:
                logger.warning("txtai save failed", exc_info=True)

    async def index(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Index documents (full rebuild for zone_id)."""
        if not documents:
            return 0
        await self.startup()

        stamped = _stamp_zone_id(documents, zone_id)
        rows = [(doc["id"], doc, None) for doc in stamped]
        async with self._lock:
            if not self._embeddings:
                return 0
            await asyncio.to_thread(self._embeddings.index, rows)
            await asyncio.to_thread(self._save)
        return len(rows)

    async def upsert(self, documents: list[dict[str, Any]], *, zone_id: str) -> int:
        """Upsert documents (insert-or-update).

        Falls back to ``index()`` on first call when the ANN backend
        hasn't been initialized yet. Subsequent calls use upsert() which
        appends with auto-incrementing indexids (no collision with pgvector).
        """
        if not documents:
            return 0
        await self.startup()

        stamped = _stamp_zone_id(documents, zone_id)
        rows = [(doc["id"], doc, None) for doc in stamped]

        async with self._lock:
            if not self._embeddings:
                return 0
            if getattr(self._embeddings, "ann", None) is None:
                await asyncio.to_thread(self._embeddings.index, rows)
            else:
                await asyncio.to_thread(self._embeddings.upsert, rows)
            await asyncio.to_thread(self._save)
        return len(rows)

    async def delete(self, ids: list[str], *, zone_id: str) -> int:  # noqa: ARG002
        """Delete documents by id."""
        if not ids:
            return 0
        await self.startup()

        async with self._lock:
            if not self._embeddings:
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
        self.last_rerank_ms = 0.0
        await self.startup()

        # Over-fetch when reranker is available so it has enough candidates.
        # 2x balances rerank quality vs CPU latency (~10ms per candidate).
        fetch_limit = limit * 2 if self._reranker else limit

        sql = _build_search_sql(query, zone_id=zone_id, path_filter=path_filter, limit=fetch_limit)
        async with self._lock:
            if not self._embeddings:
                return []
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

        texts = [_truncate_reranker_text(r.chunk_text) for r in results if r.chunk_text]
        if not texts:
            return results[:limit]

        # txtai Similarity returns [(index, score), ...] sorted by score desc
        async with self._lock:
            if not self._reranker:
                return results[:limit]
            try:
                scored: list[tuple[int, float]] = await asyncio.to_thread(
                    self._reranker,
                    query,
                    texts,
                )
            except Exception as exc:
                logger.warning("Reranker failed, falling back to backend ranking: %s", exc)
                return results[:limit]

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
        path_filter: str | None = None,
    ) -> list[BaseSearchResult]:
        """Graph-augmented search using txtai's semantic graph.

        Uses ``Embeddings.search()`` with graph enabled — txtai automatically
        applies the semantic graph as a boosting/re-ranking signal on top of
        the standard hybrid BM25+dense retrieval. This is NOT the raw graph
        query API (``graph.search()``) which expects graph query syntax.
        """
        await self.startup()

        # Build SQL with zone_id + optional path filter (same as regular search)
        sql = _build_search_sql(query, zone_id=zone_id, path_filter=path_filter, limit=limit)

        async with self._lock:
            if not self._embeddings or not getattr(self._embeddings, "graph", None):
                return []
            # txtai's Embeddings.search() uses graph as boost when graph is configured
            raw: list[dict[str, Any]] = await asyncio.to_thread(self._embeddings.search, sql)

        results: list[BaseSearchResult] = []
        for r in raw:
            score = float(r.get("score", 0.0))
            results.append(
                BaseSearchResult(
                    path=r.get("path", ""),
                    chunk_text=r.get("text", ""),
                    score=score,
                    keyword_score=score,
                    vector_score=score,
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
        await self.startup()
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
        # Note: txtai's SQL parser does not support the ESCAPE keyword.
        # We rely on _escape_sql_string to sanitise the value instead.
        clauses.append(f"path LIKE '{_escape_sql_string(path_filter)}%'")

    where = " AND ".join(clauses)
    safe_limit = max(1, min(int(limit), 1000))
    return f"SELECT id, text, score, path, zone_id FROM txtai WHERE {where} LIMIT {safe_limit}"


def _stamp_zone_id(documents: list[dict[str, Any]], zone_id: str) -> list[dict[str, Any]]:
    """Return new document list with zone_id stamped on each (immutable)."""
    return [{**doc, "zone_id": zone_id} for doc in documents]


def _truncate_reranker_text(text: str) -> str:
    """Trim reranker candidates to avoid cross-encoder sequence overflows."""
    trimmed = text.strip()
    if len(trimmed) > _RERANK_MAX_CHARS:
        trimmed = trimmed[:_RERANK_MAX_CHARS]

    words = trimmed.split()
    if len(words) > _RERANK_MAX_WORDS:
        trimmed = " ".join(words[:_RERANK_MAX_WORDS])

    return trimmed


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
