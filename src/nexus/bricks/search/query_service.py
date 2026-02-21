"""QueryService — unified search execution for keyword, semantic, and hybrid modes.

Consolidates search logic from ``semantic.py`` and ``async_search.py`` into a
single async-first service with dependency injection for fusion, ranking, and
adaptive-k strategies.

Issue: #1094 (parallel indexing pipeline)
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nexus.bricks.search.embeddings import EmbeddingProvider
from nexus.bricks.search.fusion import FusionConfig, FusionMethod, fuse_results
from nexus.bricks.search.ranking import RankingConfig, apply_attribute_boosting
from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.vector_db import VectorDatabase

logger = logging.getLogger(__name__)

_VALID_SEARCH_MODES = frozenset({"keyword", "semantic", "hybrid"})


def _dict_to_result(result: dict[str, Any]) -> BaseSearchResult:
    """Convert a canonical result dict to a ``BaseSearchResult``."""
    return BaseSearchResult(
        path=result.get("path", ""),
        chunk_text=result.get("chunk_text", ""),
        score=result.get("score", 0.0),
        chunk_index=result.get("chunk_index", 0),
        start_offset=result.get("start_offset"),
        end_offset=result.get("end_offset"),
        line_start=result.get("line_start"),
        line_end=result.get("line_end"),
        keyword_score=result.get("keyword_score"),
        vector_score=result.get("vector_score"),
        matched_field=result.get("matched_field"),
        attribute_boost=result.get("attribute_boost"),
        original_score=result.get("original_score"),
    )


class QueryService:
    """Async-first search execution service with DI for fusion/ranking/context.

    All constructor parameters are keyword-only (decision 4A).
    """

    def __init__(
        self,
        *,
        vector_db: VectorDatabase,
        session_factory: Any,
        embedding_provider: EmbeddingProvider | None = None,
        ranking_config: RankingConfig | None = None,
        ranking_fn: Callable[..., list[dict[str, Any]]] | None = None,
        fusion_fn: Callable[..., list[dict[str, Any]]] | None = None,
        context_builder: Any | None = None,
    ) -> None:
        self._vector_db = vector_db
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._ranking_config = ranking_config or RankingConfig()
        self._ranking_fn = ranking_fn or apply_attribute_boosting
        self._fusion_fn = fusion_fn or fuse_results
        self._context_builder = context_builder

    @property
    def embedding_provider(self) -> EmbeddingProvider | None:
        """Public accessor for SearchableProtocol conformance."""
        return self._embedding_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        search_mode: str = "semantic",
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
    ) -> list[BaseSearchResult]:
        """Execute a search and return typed result objects.

        Args:
            query: Natural-language search query.
            path: Root path to restrict results (``"/"`` = all).
            limit: Maximum results (k_base when adaptive_k is True).
            search_mode: ``"keyword"``, ``"semantic"``, or ``"hybrid"``.
            alpha: Vector weight for hybrid fusion (0 = BM25, 1 = vector).
            fusion_method: ``"rrf"``, ``"weighted"``, or ``"rrf_weighted"``.
            adaptive_k: Dynamically adjust limit via query complexity.

        Returns:
            Ranked list of ``BaseSearchResult`` objects.

        Raises:
            ValueError: On unknown search_mode or missing providers.
        """
        self._validate_search_mode(search_mode)
        limit = self._apply_adaptive_k(query, limit, adaptive_k)
        path_filter = path if path != "/" else None

        with self._session_factory() as session:
            if search_mode == "keyword":
                raw = self._keyword_search(session, query, limit, path_filter)
            elif search_mode == "hybrid":
                raw = await self._hybrid_search(
                    session,
                    query,
                    limit,
                    path_filter,
                    alpha,
                    fusion_method,
                )
            else:
                raw = await self._semantic_search(session, query, limit, path_filter)

        raw = self._apply_ranking(raw, query)
        return [_dict_to_result(r) for r in raw]

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _keyword_search(
        self,
        session: Any,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        return self._vector_db.keyword_search(
            session,
            query,
            limit=limit,
            path_filter=path_filter,
        )

    async def _semantic_search(
        self,
        session: Any,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        self._require_embedding_provider("Semantic")
        self._require_vector_extension("Semantic")

        assert self._embedding_provider is not None  # type narrowing
        query_embedding = await self._embedding_provider.embed_text(query)
        return self._vector_db.vector_search(
            session,
            query_embedding,
            limit=limit,
            path_filter=path_filter,
        )

    async def _hybrid_search(
        self,
        session: Any,
        query: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
    ) -> list[dict[str, Any]]:
        self._require_embedding_provider("Hybrid")
        self._require_vector_extension("Hybrid")

        assert self._embedding_provider is not None  # type narrowing
        over_fetch = limit * 3

        # Parallel: embed query + keyword search (in separate thread with own session)
        embedding_task = self._embedding_provider.embed_text(query)

        def _keyword_in_own_session() -> list[dict[str, Any]]:
            with self._session_factory() as thread_session:
                return self._vector_db.keyword_search(
                    thread_session,
                    query,
                    over_fetch,
                    path_filter,
                )

        keyword_task = asyncio.to_thread(_keyword_in_own_session)
        query_embedding, keyword_results = await asyncio.gather(
            embedding_task,
            keyword_task,
            return_exceptions=True,
        )

        if isinstance(query_embedding, BaseException):
            logger.error("Embedding generation failed: %s", query_embedding)
            raise query_embedding
        if isinstance(keyword_results, BaseException):
            logger.warning(
                "Keyword search failed in hybrid mode; continuing vector-only: %s",
                keyword_results,
            )
            keyword_results = []

        # Sequential vector search (needs the embedding, uses caller's session)
        vector_results = self._vector_db.vector_search(
            session,
            query_embedding,
            over_fetch,
            path_filter,
        )

        config = FusionConfig(
            method=FusionMethod(fusion_method),
            alpha=alpha,
            rrf_k=60,
        )
        return self._fusion_fn(
            keyword_results,
            vector_results,
            config=config,
            limit=limit,
            id_key="chunk_id",
        )

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    def _apply_ranking(
        self,
        results: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        if not results:
            return results
        if self._ranking_config.enable_attribute_boosting:
            return self._ranking_fn(results, query, self._ranking_config)
        return results

    # ------------------------------------------------------------------
    # Adaptive-k (lazy-init context builder)
    # ------------------------------------------------------------------

    def _apply_adaptive_k(self, query: str, limit: int, adaptive_k: bool) -> int:
        if not adaptive_k:
            return limit

        builder = self._get_or_create_context_builder()
        if builder is None:
            logger.debug(
                "Adaptive-k requested but context builder unavailable; using original limit=%d",
                limit,
            )
            return limit

        adaptive_config = getattr(builder, "adaptive_config", None)
        if adaptive_config is not None and not adaptive_config.enabled:
            return limit

        new_limit = builder.calculate_k_dynamic(query, k_base=limit)
        if new_limit != limit:
            logger.info(
                "[QUERY-SERVICE] Adaptive k: %d -> %d for query: %.50s",
                limit,
                new_limit,
                query,
            )
        return int(new_limit)

    def _get_or_create_context_builder(self) -> Any:
        """Return the injected AdaptiveKProtocol provider, or None.

        Issue #2036: Replaced lazy import of ``nexus.services.llm_context_builder``
        with protocol-based DI.  The ``context_builder`` is now injected via the
        constructor by the composition root (factory.py).
        """
        return self._context_builder

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_search_mode(search_mode: str) -> None:
        if search_mode not in _VALID_SEARCH_MODES:
            raise ValueError(
                f"Unknown search_mode '{search_mode}'. "
                f"Must be one of: {', '.join(sorted(_VALID_SEARCH_MODES))}"
            )

    def _require_embedding_provider(self, mode_label: str) -> None:
        if self._embedding_provider is None:
            raise ValueError(
                f"{mode_label} search requires an embedding provider. "
                "Pass embedding_provider= to QueryService or use "
                "search_mode='keyword' for FTS-only search."
            )

    def _require_vector_extension(self, mode_label: str) -> None:
        if not self._vector_db.vec_available:
            raise ValueError(
                f"{mode_label} search requires a vector database extension. "
                "Install sqlite-vec or pgvector, then call vector_db.initialize(). "
                "Use search_mode='keyword' for FTS-only search."
            )
