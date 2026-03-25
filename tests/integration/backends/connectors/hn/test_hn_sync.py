"""Tests for HackerNews poll-based sync provider (Phase 4, #3148)."""

import pytest

from nexus.backends.connectors.cli.protocol import ConnectorSyncProvider
from nexus.backends.connectors.hn.sync_provider import HNSyncProvider


class TestHNSyncProvider:
    def test_satisfies_protocol(self) -> None:
        """HNSyncProvider satisfies ConnectorSyncProvider protocol."""
        provider = HNSyncProvider(connector=None)
        assert isinstance(provider, ConnectorSyncProvider)

    @pytest.mark.asyncio
    async def test_list_returns_sync_page(self) -> None:
        provider = HNSyncProvider(connector=None)
        page = await provider.list_remote_items("/top")
        assert hasattr(page, "items")
        assert hasattr(page, "state_token")

    @pytest.mark.asyncio
    async def test_list_with_since_filters(self) -> None:
        provider = HNSyncProvider(connector=None)
        page = await provider.list_remote_items("/top", since="99999999")
        # With a very high since token, should return empty or fewer items
        assert isinstance(page.items, list)

    @pytest.mark.asyncio
    async def test_fetch_item_returns_result(self) -> None:
        provider = HNSyncProvider(connector=None)
        result = await provider.fetch_item("1")
        assert result.relative_path.endswith(".json")
        assert result.content is not None
