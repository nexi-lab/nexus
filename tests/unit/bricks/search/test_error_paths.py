"""Tests for search brick error paths (Issue #1520).

Validates error handling at brick boundaries:
- VectorDatabase.initialize with unsupported dialect
- SearchDaemon.search when not initialized
- verify_imports with missing modules
- SemanticSearch.search when embedding_provider is None
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# VectorDatabase error paths
# =============================================================================


class TestVectorDatabaseErrors:
    """Test VectorDatabase error conditions."""

    def test_default_is_sqlite(self) -> None:
        """VectorDatabase defaults to SQLite (is_postgresql=False)."""
        from nexus.bricks.search.vector_db import VectorDatabase

        engine = MagicMock()
        vdb = VectorDatabase(engine)
        assert vdb.db_type == "sqlite"

    def test_postgresql_flag(self) -> None:
        """VectorDatabase with is_postgresql=True reports postgresql."""
        from nexus.bricks.search.vector_db import VectorDatabase

        engine = MagicMock()
        vdb = VectorDatabase(engine, is_postgresql=True)
        assert vdb.db_type == "postgresql"


# =============================================================================
# SearchDaemon error paths
# =============================================================================


class TestSearchDaemonErrors:
    """Test SearchDaemon error conditions."""

    @pytest.mark.asyncio
    async def test_search_when_not_initialized(self) -> None:
        """SearchDaemon.search should raise RuntimeError when not initialized."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        assert not daemon.is_initialized

        with pytest.raises(RuntimeError, match="SearchDaemon not initialized"):
            await daemon.search("test query")

    @pytest.mark.asyncio
    async def test_double_startup_warns(self) -> None:
        """SearchDaemon.startup called twice should log warning but not crash."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()

        # First startup - mock internals
        with (
            patch.object(daemon, "_init_bm25s_index", return_value=None),
            patch.object(daemon, "_init_database_pool", return_value=None),
            patch.object(daemon, "_check_zoekt", return_value=None),
            patch.object(daemon, "_check_embedding_cache", return_value=None),
        ):
            await daemon.startup()
            assert daemon.is_initialized

            # Second startup should just return (warning logged)
            await daemon.startup()
            assert daemon.is_initialized

        # Cleanup
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self) -> None:
        """Multiple shutdown calls should not crash."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        await daemon.shutdown()
        await daemon.shutdown()  # Should not raise


# =============================================================================
# verify_imports error paths
# =============================================================================


class TestVerifyImportsErrors:
    """Test verify_imports with missing modules."""

    def test_verify_imports_returns_dict(self) -> None:
        """verify_imports should always return a dict."""
        from nexus.bricks.search.manifest import verify_imports

        result = verify_imports()
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_verify_imports_required_modules(self) -> None:
        """Required modules should be True when available."""
        from nexus.bricks.search.manifest import verify_imports

        result = verify_imports()
        # These core modules should always be importable (Issue #2075: CQRS split)
        assert result.get("nexus.bricks.search.query_service") is True
        assert result.get("nexus.bricks.search.indexing_service") is True
        assert result.get("nexus.bricks.search.fusion") is True
        assert result.get("nexus.bricks.search.chunking") is True
        assert result.get("nexus.bricks.search.embeddings") is True
        assert result.get("nexus.bricks.search.vector_db") is True

    def test_verify_imports_optional_missing(self) -> None:
        """Optional modules may be False without error."""
        from nexus.bricks.search.manifest import verify_imports

        result = verify_imports()
        # Optional modules are allowed to be False
        for key in result:
            assert isinstance(result[key], bool)

    def test_verify_imports_with_patched_missing_module(self) -> None:
        """Simulate a required module being unavailable."""
        import importlib.util
        from unittest.mock import patch

        from nexus.bricks.search.manifest import verify_imports

        original_find_spec = importlib.util.find_spec

        def mock_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "nexus.bricks.search.query_service":
                return None
            return original_find_spec(name, *args, **kwargs)

        with patch("importlib.util.find_spec", side_effect=mock_find_spec):
            result = verify_imports()
            assert result["nexus.bricks.search.query_service"] is False


# =============================================================================
# SemanticSearch error paths
# =============================================================================


class TestQueryServiceErrors:
    """Test QueryService error conditions (migrated from SemanticSearch, Issue #2075)."""

    @pytest.mark.asyncio
    async def test_semantic_search_without_embedding_provider(self) -> None:
        """Semantic search mode should raise when no embedding provider."""
        from nexus.bricks.search.query_service import QueryService

        vector_db = MagicMock()
        vector_db.vec_available = True
        session_factory = MagicMock()

        qs = QueryService(
            vector_db=vector_db,
            session_factory=session_factory,
            embedding_provider=None,
        )

        with pytest.raises(ValueError, match="requires an embedding provider"):
            await qs.search("test", search_mode="semantic")

    @pytest.mark.asyncio
    async def test_hybrid_search_without_embedding_provider(self) -> None:
        """Hybrid search mode should raise when no embedding provider."""
        from nexus.bricks.search.query_service import QueryService

        vector_db = MagicMock()
        vector_db.vec_available = True
        session_factory = MagicMock()

        qs = QueryService(
            vector_db=vector_db,
            session_factory=session_factory,
            embedding_provider=None,
        )

        with pytest.raises(ValueError, match="requires an embedding provider"):
            await qs.search("test", search_mode="hybrid")

    @pytest.mark.asyncio
    async def test_hybrid_search_without_vec_extension(self) -> None:
        """Hybrid search should raise when vector extension unavailable."""
        from nexus.bricks.search.query_service import QueryService

        vector_db = MagicMock()
        vector_db.vec_available = False
        session_factory = MagicMock()
        emb = MagicMock()

        qs = QueryService(
            vector_db=vector_db,
            session_factory=session_factory,
            embedding_provider=emb,
        )

        with pytest.raises(ValueError, match="requires a vector database extension"):
            await qs.search("test", search_mode="hybrid")


# =============================================================================
# SearchBrickManifest validation
# =============================================================================


class TestSearchBrickManifest:
    """Test SearchBrickManifest dataclass."""

    def test_manifest_defaults(self) -> None:
        from nexus.bricks.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        assert m.name == "search"
        assert m.protocol == "SearchBrickProtocol"
        assert m.version == "1.0.0"
        assert isinstance(m.config_schema, dict)
        assert isinstance(m.dependencies, tuple)

    def test_manifest_is_frozen(self) -> None:
        from nexus.bricks.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        with pytest.raises(AttributeError):
            m.name = "other"  # type: ignore[misc]

    def test_manifest_config_schema_has_expected_keys(self) -> None:
        from nexus.bricks.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        assert "embedding_provider" in m.config_schema
        assert "search_mode" in m.config_schema
        assert "entropy_filtering" in m.config_schema
        assert "fusion_method" in m.config_schema
