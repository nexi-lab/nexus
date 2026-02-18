"""Tests for search brick error paths (Issue #1520).

Validates error handling at brick boundaries:
- VectorDatabase.initialize with unsupported dialect
- SearchDaemon.search when not initialized
- verify_imports with missing modules
- SemanticSearch.search when embedding_provider is None
"""


from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# VectorDatabase error paths
# =============================================================================


class TestVectorDatabaseErrors:
    """Test VectorDatabase error conditions."""

    def test_initialize_unsupported_dialect(self) -> None:
        """VectorDatabase.initialize should raise for unsupported dialect."""
        from nexus.search.vector_db import VectorDatabase

        engine = MagicMock()
        engine.dialect.name = "mysql"

        vdb = VectorDatabase(engine)
        with pytest.raises(ValueError, match="Unsupported database type: mysql"):
            vdb.initialize()

    def test_vector_search_unsupported_dialect(self) -> None:
        """vector_search should raise for unsupported dialect."""
        from nexus.search.vector_db import VectorDatabase

        engine = MagicMock()
        engine.dialect.name = "oracle"

        vdb = VectorDatabase(engine)
        session = MagicMock()

        with pytest.raises(ValueError, match="Unsupported database type"):
            vdb.vector_search(session, [0.1, 0.2], limit=5)

    def test_keyword_search_unsupported_dialect(self) -> None:
        """keyword_search should raise for unsupported dialect (FTS fallback)."""
        from nexus.search.vector_db import VectorDatabase

        engine = MagicMock()
        engine.dialect.name = "oracle"

        vdb = VectorDatabase(engine)
        vdb._initialized = True
        session = MagicMock()

        with (
            patch.object(vdb, "_try_keyword_search_with_zoekt", return_value=None),
            patch.object(vdb, "_try_keyword_search_with_bm25s", return_value=None),
            pytest.raises(ValueError, match="Unsupported database type"),
        ):
            vdb.keyword_search(session, "test", limit=5)


# =============================================================================
# SearchDaemon error paths
# =============================================================================


class TestSearchDaemonErrors:
    """Test SearchDaemon error conditions."""

    @pytest.mark.asyncio
    async def test_search_when_not_initialized(self) -> None:
        """SearchDaemon.search should raise RuntimeError when not initialized."""
        from nexus.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        assert not daemon.is_initialized

        with pytest.raises(RuntimeError, match="SearchDaemon not initialized"):
            await daemon.search("test query")

    @pytest.mark.asyncio
    async def test_double_startup_warns(self) -> None:
        """SearchDaemon.startup called twice should log warning but not crash."""
        from nexus.search.daemon import SearchDaemon

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
        from nexus.search.daemon import SearchDaemon

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
        from nexus.search.manifest import verify_imports

        result = verify_imports()
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_verify_imports_required_modules(self) -> None:
        """Required modules should be True when available."""
        from nexus.search.manifest import verify_imports

        result = verify_imports()
        # These core modules should always be importable
        assert result.get("nexus.search.semantic") is True
        assert result.get("nexus.search.fusion") is True
        assert result.get("nexus.search.chunking") is True
        assert result.get("nexus.search.embeddings") is True
        assert result.get("nexus.search.vector_db") is True

    def test_verify_imports_optional_missing(self) -> None:
        """Optional modules may be False without error."""
        from nexus.search.manifest import verify_imports

        result = verify_imports()
        # Optional modules are allowed to be False
        for key in result:
            assert isinstance(result[key], bool)

    def test_verify_imports_with_patched_missing_module(self) -> None:
        """Simulate a required module being unavailable."""
        import importlib
        from unittest.mock import patch

        from nexus.search.manifest import verify_imports

        original_import = importlib.import_module

        def mock_import(name: str) -> Any:
            if name == "nexus.search.async_search":
                raise ImportError(f"No module named '{name}'")
            return original_import(name)

        with patch("importlib.import_module", side_effect=mock_import):
            result = verify_imports()
            assert result["nexus.search.async_search"] is False


# =============================================================================
# SemanticSearch error paths
# =============================================================================


class TestSemanticSearchErrors:
    """Test SemanticSearch error conditions."""

    def test_init_requires_engine(self) -> None:
        """SemanticSearch should raise RuntimeError without engine."""
        from nexus.search.semantic import SemanticSearch

        nx = MagicMock()
        with pytest.raises(RuntimeError, match="requires a SQL engine"):
            SemanticSearch(nx=nx, engine=None)

    @pytest.mark.asyncio
    async def test_semantic_search_without_embedding_provider(self) -> None:
        """Semantic search mode should raise when no embedding provider."""
        from nexus.search.semantic import SemanticSearch

        nx = MagicMock()
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        ss = SemanticSearch(nx=nx, engine=engine, embedding_provider=None)

        with pytest.raises(ValueError, match="requires an embedding provider"):
            await ss.search("test", search_mode="semantic")

    @pytest.mark.asyncio
    async def test_hybrid_search_without_embedding_provider(self) -> None:
        """Hybrid search mode should raise when no embedding provider."""
        from nexus.search.semantic import SemanticSearch

        nx = MagicMock()
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        ss = SemanticSearch(nx=nx, engine=engine, embedding_provider=None)

        with pytest.raises(ValueError, match="requires an embedding provider"):
            await ss.search("test", search_mode="hybrid")

    @pytest.mark.asyncio
    async def test_hybrid_search_without_vec_extension(self) -> None:
        """Hybrid search should raise when vector extension unavailable."""
        from nexus.search.semantic import SemanticSearch

        nx = MagicMock()
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        emb = MagicMock()
        ss = SemanticSearch(nx=nx, engine=engine, embedding_provider=emb)
        ss.vector_db.vec_available = False

        with pytest.raises(ValueError, match="requires vector database extension"):
            await ss.search("test", search_mode="hybrid")


# =============================================================================
# SearchBrickManifest validation
# =============================================================================


class TestSearchBrickManifest:
    """Test SearchBrickManifest dataclass."""

    def test_manifest_defaults(self) -> None:
        from nexus.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        assert m.name == "search"
        assert m.protocol == "SearchBrickProtocol"
        assert m.version == "1.0.0"
        assert isinstance(m.config_schema, dict)
        assert isinstance(m.dependencies, list)

    def test_manifest_is_frozen(self) -> None:
        from nexus.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        with pytest.raises(AttributeError):
            m.name = "other"  # type: ignore[misc]

    def test_manifest_config_schema_has_expected_keys(self) -> None:
        from nexus.search.manifest import SearchBrickManifest

        m = SearchBrickManifest()
        assert "embedding_provider" in m.config_schema
        assert "search_mode" in m.config_schema
        assert "entropy_filtering" in m.config_schema
        assert "fusion_method" in m.config_schema
