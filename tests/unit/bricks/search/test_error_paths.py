"""Tests for search brick error paths (Issue #1520, #2663).

Validates error handling at brick boundaries:
- SearchDaemon.search when not initialized
- verify_imports with missing modules
- SearchBrickManifest validation
"""

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
