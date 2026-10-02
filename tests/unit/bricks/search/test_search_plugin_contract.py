"""SearchService preserves the plugin's query contract and failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.types import OperationContext


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "stats"])
async def test_missing_plugin_does_not_query_record_store(operation: str) -> None:
    records = MagicMock()
    service = SearchService(metadata_store=MagicMock(), record_store=records)
    try:
        with pytest.raises(ValueError, match="search-plugin"):
            if operation == "query":
                await service.semantic_search("confidential", search_mode="keyword")
            else:
                await service.semantic_search_stats()
        records.session_factory.assert_not_called()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_plugin_query_error_is_not_an_empty_result() -> None:
    service = SearchService(metadata_store=MagicMock(), enforce_permissions=False)
    daemon = MagicMock()
    daemon.search = AsyncMock(return_value=[])
    daemon.search_with_error = AsyncMock(return_value=([], "embedding provider unavailable"))
    service._search_daemon = daemon
    try:
        with pytest.raises(RuntimeError, match="embedding provider unavailable"):
            await service.semantic_search("design", search_mode="semantic")
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [(("legal", "w"),), (("legal", "w"), ("engineering", "r"))])
async def test_write_only_context_never_queries_plugin(grants: tuple[tuple[str, str], ...]) -> None:
    service = SearchService(metadata_store=MagicMock(), enforce_permissions=False)
    daemon = MagicMock()
    daemon.search = AsyncMock(return_value=[])
    daemon.search_with_error = AsyncMock(return_value=([], None))
    service._search_daemon = daemon
    context = OperationContext(user_id="alice", groups=[], zone_id="legal", zone_perms=grants)
    try:
        assert await service.semantic_search("private", context=context) == []
        daemon.search.assert_not_called()
        daemon.search_with_error.assert_not_called()
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [(("legal", "r"),), (("root", "r"),)])
async def test_readable_context_reaches_plugin(grants: tuple[tuple[str, str], ...]) -> None:
    service = SearchService(metadata_store=MagicMock(), enforce_permissions=False)
    daemon = MagicMock()
    daemon.search_with_error = AsyncMock(return_value=([], None))
    service._search_daemon = daemon
    context = OperationContext(user_id="alice", groups=[], zone_id="legal", zone_perms=grants)
    try:
        assert await service.semantic_search("design", context=context) == []
        daemon.search_with_error.assert_awaited_once()
        assert daemon.search_with_error.call_args.args[0].zone_id == "legal"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_federated_outage_preserves_failures_without_local_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.bricks.search.search_degraded import FederatedSearchResponse, ZoneFailure
    from nexus.server.api.v2.routers.search import _handle_federated_search

    dispatcher = MagicMock()
    dispatcher.search = AsyncMock(
        return_value=FederatedSearchResponse(
            results=[],
            zones_searched=["legal"],
            zones_failed=[ZoneFailure(zone_id="legal", error="unreachable")],
        )
    )
    monkeypatch.setattr(
        "nexus.bricks.search.federated_search.FederatedSearchDispatcher",
        lambda **kwargs: dispatcher,
    )
    nexus = MagicMock()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    rebac_service=object(), deployment_profile="sandbox", nexus_fs=nexus
                )
            ),
        }
    )
    result = await _handle_federated_search(
        q="private",
        search_type="semantic",
        limit=5,
        path_filter=None,
        alpha=0.5,
        fusion_method="rrf",
        rrf_k=60,
        auth_result={"user_id": "alice"},
        search_daemon=MagicMock(),
        request=request,
    )
    assert result["results"] == []
    assert result["zones_failed"] == [{"zone_id": "legal", "error": "unreachable"}]
    nexus.service.assert_not_called()
