from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.search_service import SearchService


@pytest.mark.asyncio
async def test_semantic_search_stats_prefers_search_daemon() -> None:
    service = SearchService(metadata_store=MagicMock())
    service._search_daemon = MagicMock()
    service._search_daemon.get_stats.return_value = {
        "backend": "txtai",
        "bm25_documents": 12,
    }

    stats = await service.semantic_search_stats()

    assert stats["backend"] == "txtai"
    assert stats["engine"] == "txtai"
    assert stats["bm25_documents"] == 12
