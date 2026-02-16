"""Tests for AsyncSemanticSearch session_factory DI (Issue #1597)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.search.async_search import AsyncSemanticSearch


class TestAsyncSemanticSearchSessionFactory:
    """Tests for async_session_factory dependency injection (Issue #1597)."""

    def test_default_creates_own_engine(self) -> None:
        """Test that omitting async_session_factory creates a private engine."""
        search = AsyncSemanticSearch(
            database_url="sqlite:///test.db",
        )

        assert search.engine is not None
        assert search.async_session is not None

    def test_injected_factory_skips_engine_creation(self) -> None:
        """Test that providing async_session_factory skips engine creation."""
        mock_factory = MagicMock()

        search = AsyncSemanticSearch(
            database_url="sqlite:///test.db",
            async_session_factory=mock_factory,
        )

        # Engine should be None (owned externally)
        assert search.engine is None
        # Session factory should be the injected one
        assert search.async_session is mock_factory

    @pytest.mark.asyncio
    async def test_close_noop_with_injected_factory(self) -> None:
        """Test that close() is safe when engine is None (injected path)."""
        mock_factory = MagicMock()

        search = AsyncSemanticSearch(
            database_url="sqlite:///test.db",
            async_session_factory=mock_factory,
        )

        # Should not raise
        await search.close()

    @pytest.mark.asyncio
    async def test_close_disposes_engine_when_owned(self) -> None:
        """Test that close() disposes the engine when created internally."""
        search = AsyncSemanticSearch(
            database_url="sqlite:///test.db",
        )

        # Replace engine with a mock to verify dispose is called
        mock_engine = AsyncMock()
        search.engine = mock_engine

        await search.close()

        mock_engine.dispose.assert_awaited_once()
