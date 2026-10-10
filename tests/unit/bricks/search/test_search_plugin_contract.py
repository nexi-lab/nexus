"""SearchService preserves the plugin's query contract and failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request


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
