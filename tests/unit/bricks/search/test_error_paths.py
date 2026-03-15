"""Tests for search brick error paths (Issue #1520, #2663).

Validates error handling at brick boundaries:
- SearchDaemon.search when not initialized
- verify_imports with missing modules
- SearchBrickManifest validation
"""

import logging
from unittest.mock import AsyncMock

import pytest

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
    async def test_shutdown_idempotent(self) -> None:
        """Multiple shutdown calls should not crash."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        await daemon.shutdown()
        await daemon.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_semantic_search_without_embedding_provider_logs_debug_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing legacy embedding provider should be treated as expected fallback."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        daemon._async_engine = object()
        daemon._async_session = object()

        with caplog.at_level(logging.DEBUG):
            results = await daemon._semantic_search("test query", 5, None)

        assert results == []
        assert "Legacy semantic search unavailable: no embedding provider configured" in caplog.text
        assert "Could not generate query embedding" not in caplog.text

    @pytest.mark.asyncio
    async def test_index_documents_uses_backend_upsert(self) -> None:
        """Explicit indexing should delegate to the active backend."""
        from nexus.bricks.search.daemon import SearchDaemon
        from nexus.contracts.constants import ROOT_ZONE_ID

        daemon = SearchDaemon()
        daemon._initialized = True
        daemon._backend = AsyncMock()
        daemon._backend.upsert.return_value = 1

        docs = [{"id": "doc-1", "text": "hello", "path": "/skill-hub/search/doc.md"}]
        count = await daemon.index_documents(docs)

        assert count == 1
        daemon._backend.upsert.assert_awaited_once_with(docs, zone_id=ROOT_ZONE_ID)
        assert daemon.stats.last_index_refresh is not None

    @pytest.mark.asyncio
    async def test_delete_documents_uses_backend_delete(self) -> None:
        """Explicit deletion should delegate to the active backend."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        daemon._initialized = True
        daemon._backend = AsyncMock()
        daemon._backend.delete.return_value = 2

        count = await daemon.delete_documents(["doc-1", "doc-2"], zone_id="corp")

        assert count == 2
        daemon._backend.delete.assert_awaited_once_with(["doc-1", "doc-2"], zone_id="corp")
        assert daemon.stats.last_index_refresh is not None


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

    def test_verify_imports_optional_missing(self) -> None:
        """Optional modules may be False without error."""
        from nexus.bricks.search.manifest import verify_imports

        result = verify_imports()
        for key in result:
            assert isinstance(result[key], bool)


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
