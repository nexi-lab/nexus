"""TDD tests for QueryService (Issue #1094).

Tests the unified search execution service covering keyword, semantic,
and hybrid search modes with mock dependencies. Verifies ranking,
adaptive-k, and error-handling paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.fusion import FusionConfig, FusionMethod
from nexus.bricks.search.query_service import QueryService
from nexus.bricks.search.ranking import RankingConfig
from nexus.bricks.search.results import BaseSearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_result_dict(
    path: str = "test.py",
    score: float = 0.9,
    chunk_text: str = "sample chunk text",
    chunk_index: int = 0,
) -> dict:
    """Return a canonical result dict as produced by VectorDatabase methods."""
    return {
        "path": path,
        "chunk_text": chunk_text,
        "score": score,
        "chunk_index": chunk_index,
        "start_offset": 0,
        "end_offset": len(chunk_text),
        "line_start": 1,
        "line_end": 1,
        "chunk_id": f"{path}:{chunk_index}",
    }


def _mock_vector_db(vec_available: bool = True) -> MagicMock:
    """Return a mock VectorDatabase with keyword_search and vector_search."""
    vdb = MagicMock()
    vdb.vec_available = vec_available
    vdb.keyword_search = MagicMock(
        return_value=[
            _mock_result_dict("a.py", 0.8, "keyword hit a", 0),
            _mock_result_dict("b.py", 0.6, "keyword hit b", 1),
        ],
    )
    vdb.vector_search = MagicMock(
        return_value=[
            _mock_result_dict("c.py", 0.95, "vector hit c", 0),
            _mock_result_dict("d.py", 0.85, "vector hit d", 1),
        ],
    )
    return vdb


def _mock_session_factory() -> MagicMock:
    """Return a callable that returns a synchronous context manager yielding a mock session."""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory


def _mock_embedding_provider(dim: int = 4) -> MagicMock:
    """Return an embedding provider with async embed_text returning a deterministic vector."""
    provider = MagicMock()
    provider.__class__.__name__ = "MockEmbeddingProvider"

    async def _embed_text(text: str) -> list[float]:
        return [0.1 * i for i in range(dim)]

    provider.embed_text = AsyncMock(side_effect=_embed_text)
    return provider


def _build_service(
    *,
    vector_db: MagicMock | None = None,
    session_factory: MagicMock | None = None,
    embedding_provider: MagicMock | None = None,
    ranking_config: RankingConfig | None = None,
    ranking_fn: MagicMock | None = None,
    fusion_fn: MagicMock | None = None,
    context_builder: MagicMock | None = None,
    vec_available: bool = True,
) -> QueryService:
    """Convenience builder for QueryService with sensible mock defaults."""
    vdb = vector_db or _mock_vector_db(vec_available=vec_available)
    sf = session_factory or _mock_session_factory()
    return QueryService(
        vector_db=vdb,
        session_factory=sf,
        embedding_provider=embedding_provider,
        ranking_config=ranking_config,
        ranking_fn=ranking_fn,
        fusion_fn=fusion_fn,
        context_builder=context_builder,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestKeywordSearch:
    @pytest.mark.asyncio
    async def test_keyword_search_calls_vector_db_keyword(self) -> None:
        """Keyword mode delegates to vector_db.keyword_search and returns BaseSearchResult list."""
        vdb = _mock_vector_db()
        sf = _mock_session_factory()
        service = _build_service(vector_db=vdb, session_factory=sf)

        results = await service.search("find me", path="/src", limit=5, search_mode="keyword")

        # Verify vector_db.keyword_search was called with correct arguments
        vdb.keyword_search.assert_called_once()
        call_args = vdb.keyword_search.call_args
        assert call_args[0][1] == "find me"  # query
        assert call_args[1]["limit"] == 5
        assert call_args[1]["path_filter"] == "/src"

        # Verify results are typed BaseSearchResult
        assert len(results) == 2
        assert all(isinstance(r, BaseSearchResult) for r in results)
        assert results[0].path == "a.py"
        assert results[1].path == "b.py"


class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_semantic_search_embeds_then_vector_search(self) -> None:
        """Semantic mode embeds the query then performs vector_search."""
        vdb = _mock_vector_db()
        ep = _mock_embedding_provider(dim=4)
        service = _build_service(vector_db=vdb, embedding_provider=ep)

        results = await service.search("deep meaning", search_mode="semantic")

        # embed_text called with the query
        ep.embed_text.assert_awaited_once_with("deep meaning")

        # vector_search called with the resulting embedding
        vdb.vector_search.assert_called_once()
        call_args = vdb.vector_search.call_args
        embedding_arg = call_args[0][1]
        assert isinstance(embedding_arg, list)
        assert len(embedding_arg) == 4

        # No keyword_search for semantic mode
        vdb.keyword_search.assert_not_called()

        assert len(results) == 2
        assert all(isinstance(r, BaseSearchResult) for r in results)


class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_hybrid_search_parallel_execution(self) -> None:
        """Hybrid mode invokes both keyword_search and embed_text + vector_search."""
        vdb = _mock_vector_db()
        ep = _mock_embedding_provider(dim=4)
        fusion_fn = MagicMock(return_value=[_mock_result_dict("fused.py", 0.99)])
        service = _build_service(vector_db=vdb, embedding_provider=ep, fusion_fn=fusion_fn)

        results = await service.search("hybrid query", search_mode="hybrid")

        # Both embedding and keyword paths were exercised
        ep.embed_text.assert_awaited_once_with("hybrid query")
        vdb.keyword_search.assert_called_once()
        vdb.vector_search.assert_called_once()

        assert len(results) == 1
        assert results[0].path == "fused.py"

    @pytest.mark.asyncio
    async def test_hybrid_search_fuses_keyword_and_vector(self) -> None:
        """Hybrid mode calls fusion_fn with keyword results, vector results, and FusionConfig."""
        keyword_hits = [_mock_result_dict("kw.py", 0.7)]
        vector_hits = [_mock_result_dict("vec.py", 0.95)]

        vdb = _mock_vector_db()
        vdb.keyword_search.return_value = keyword_hits
        vdb.vector_search.return_value = vector_hits

        ep = _mock_embedding_provider()
        fusion_fn = MagicMock(return_value=[_mock_result_dict("merged.py", 0.88)])
        service = _build_service(vector_db=vdb, embedding_provider=ep, fusion_fn=fusion_fn)

        await service.search(
            "fusion test",
            search_mode="hybrid",
            alpha=0.7,
            fusion_method="rrf",
        )

        fusion_fn.assert_called_once()
        call_kwargs = fusion_fn.call_args
        # Positional args: keyword_results, vector_results
        assert call_kwargs[0][0] == keyword_hits
        assert call_kwargs[0][1] == vector_hits
        # Keyword args include config
        config = call_kwargs[1]["config"]
        assert isinstance(config, FusionConfig)
        assert config.method == FusionMethod.RRF
        assert config.alpha == 0.7

    @pytest.mark.asyncio
    async def test_hybrid_keyword_failure_returns_vector_only(self) -> None:
        """If keyword_search raises, hybrid continues with empty keyword results."""
        vdb = _mock_vector_db()
        vdb.keyword_search.side_effect = RuntimeError("FTS unavailable")

        ep = _mock_embedding_provider()
        fusion_fn = MagicMock(return_value=[_mock_result_dict("vec_only.py", 0.9)])
        service = _build_service(vector_db=vdb, embedding_provider=ep, fusion_fn=fusion_fn)

        results = await service.search("fallback test", search_mode="hybrid")

        # fusion_fn called with empty keyword list and valid vector results
        fusion_fn.assert_called_once()
        kw_arg = fusion_fn.call_args[0][0]
        vec_arg = fusion_fn.call_args[0][1]
        assert kw_arg == []
        assert len(vec_arg) > 0

        assert len(results) == 1
        assert results[0].path == "vec_only.py"


class TestAdaptiveK:
    @pytest.mark.asyncio
    async def test_adaptive_k_calls_context_builder(self) -> None:
        """When adaptive_k=True, context_builder.calculate_k_dynamic adjusts the limit."""
        ctx_builder = MagicMock()
        ctx_builder.adaptive_config = MagicMock(enabled=True)
        ctx_builder.calculate_k_dynamic = MagicMock(return_value=25)

        vdb = _mock_vector_db()
        service = _build_service(
            vector_db=vdb,
            context_builder=ctx_builder,
        )

        await service.search("adaptive query", limit=10, search_mode="keyword", adaptive_k=True)

        # context_builder.calculate_k_dynamic called with query and k_base
        ctx_builder.calculate_k_dynamic.assert_called_once_with("adaptive query", k_base=10)

        # keyword_search should receive the adaptive limit (25), not the original (10)
        call_kwargs = vdb.keyword_search.call_args[1]
        assert call_kwargs["limit"] == 25


class TestRanking:
    @pytest.mark.asyncio
    async def test_attribute_boosting_applied_when_enabled(self) -> None:
        """When ranking_config.enable_attribute_boosting is True, ranking_fn is invoked."""
        ranking_config = RankingConfig(enable_attribute_boosting=True)
        ranking_fn = MagicMock(
            return_value=[_mock_result_dict("boosted.py", 1.2)],
        )

        service = _build_service(
            ranking_config=ranking_config,
            ranking_fn=ranking_fn,
        )

        results = await service.search("boost me", search_mode="keyword")

        ranking_fn.assert_called_once()
        # ranking_fn receives (results, query, ranking_config)
        args = ranking_fn.call_args[0]
        assert args[1] == "boost me"
        assert args[2] is ranking_config
        assert results[0].path == "boosted.py"


class TestValidationErrors:
    @pytest.mark.asyncio
    async def test_search_without_embedding_raises_for_semantic(self) -> None:
        """Semantic mode without embedding_provider raises ValueError."""
        service = _build_service(embedding_provider=None)

        with pytest.raises(ValueError, match="embedding provider"):
            await service.search("no embeddings", search_mode="semantic")

    @pytest.mark.asyncio
    async def test_search_without_vec_extension_raises(self) -> None:
        """Semantic mode when vec_available=False raises ValueError."""
        ep = _mock_embedding_provider()
        service = _build_service(embedding_provider=ep, vec_available=False)

        with pytest.raises(ValueError, match="vector database extension"):
            await service.search("no vectors", search_mode="semantic")


class TestResultConversion:
    @pytest.mark.asyncio
    async def test_search_returns_base_search_result_list(self) -> None:
        """All returned objects are BaseSearchResult with correct field values."""
        vdb = _mock_vector_db()
        vdb.keyword_search.return_value = [
            _mock_result_dict("alpha.py", 0.75, "chunk alpha", 0),
            _mock_result_dict("beta.py", 0.55, "chunk beta", 3),
        ]
        # Disable ranking so raw results pass through
        ranking_config = RankingConfig(enable_attribute_boosting=False)
        service = _build_service(
            vector_db=vdb,
            ranking_config=ranking_config,
        )

        results = await service.search("types check", search_mode="keyword")

        assert len(results) == 2
        assert all(isinstance(r, BaseSearchResult) for r in results)

        r0 = results[0]
        assert r0.path == "alpha.py"
        assert r0.score == 0.75
        assert r0.chunk_text == "chunk alpha"
        assert r0.chunk_index == 0
        assert r0.start_offset == 0
        assert r0.end_offset == len("chunk alpha")
        assert r0.line_start == 1
        assert r0.line_end == 1

        r1 = results[1]
        assert r1.path == "beta.py"
        assert r1.score == 0.55
        assert r1.chunk_index == 3
