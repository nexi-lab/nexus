from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.search_service import SearchService


@pytest.mark.asyncio
async def test_semantic_search_stats_prefers_search_daemon() -> None:
    service = SearchService(metadata_store=MagicMock())
    # SearchDaemon.get_stats is `async def`; the daemon proxy must be
    # mocked with AsyncMock so `await daemon.get_stats()` resolves to
    # the canned dict instead of TypeError-ing on `await dict(...)`.
    service._search_daemon = MagicMock()
    service._search_daemon.get_stats = AsyncMock(return_value={"backend": "PgFtsBackend"})

    stats = await service.semantic_search_stats()

    assert stats["backend"] == "PgFtsBackend"
    assert stats["engine"] == "PgFtsBackend"
