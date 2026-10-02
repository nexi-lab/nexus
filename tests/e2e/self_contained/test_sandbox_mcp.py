"""MCP keeps plugin failures distinct from successful empty searches."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.mcp.server import create_mcp_server
from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService
from nexus.core.nexus_fs import NexusFS


class SearchNexus:
    def __init__(self, search: SearchService) -> None:
        self.search = search

    def service(self, name: str) -> Any:
        return self.search if name == "search" else None


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["sandbox", "full"])
async def test_mcp_search_preserves_plugin_results_and_errors(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("NEXUS_PROFILE", profile)
    records = MagicMock()
    service = SearchService(
        metadata_store=MagicMock(), record_store=records, enforce_permissions=False
    )
    daemon = MagicMock()
    daemon.search_with_error = AsyncMock(
        return_value=([BaseSearchResult(path="/notes.md", chunk_text="orchid", score=0.8)], None)
    )
    service._search_daemon = daemon
    try:
        mcp = await create_mcp_server(nx=cast(NexusFS, SearchNexus(service)))
        tool = await mcp.get_tool("nexus_semantic_search")
        assert tool is not None
        response = json.loads(await tool.fn(query="orchid", limit=5, search_mode="hybrid"))
        assert [item["path"] for item in response["items"]] == ["/notes.md"]
        assert "semantic_degraded" not in response
        assert daemon.search_with_error.call_args.args[0].search_type == "hybrid"

        daemon.search_with_error.return_value = ([], None)
        empty = json.loads(await tool.fn(query="absent"))
        assert empty["items"] == []
        daemon.search_with_error.return_value = ([], "embedding provider unavailable")
        failure = await tool.fn(query="orchid")
        assert failure.startswith("Error:"), failure
        assert "embedding provider unavailable" in failure

        service._search_daemon = None
        unavailable = await tool.fn(query="orchid")
        assert unavailable.startswith("Error:"), unavailable
        assert "not available" in unavailable
        records.session_factory.assert_not_called()
    finally:
        service.close()
