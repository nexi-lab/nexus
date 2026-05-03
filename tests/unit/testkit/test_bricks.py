from __future__ import annotations

import pytest
from testkit.bricks import FakeLifecycleBrick, FakeSearchBrick, probe_service_lifecycle

from nexus.contracts.protocols.search import SearchBrickProtocol


@pytest.mark.asyncio
async def test_fake_lifecycle_brick_records_idempotent_lifecycle() -> None:
    brick = FakeLifecycleBrick(name="cache")

    await brick.start()
    await brick.start()
    assert brick.started is True
    assert brick.start_calls == 2
    assert brick.events == ("start",)
    assert await brick.health_check() is True

    await brick.stop()
    await brick.stop()
    assert brick.started is False
    assert brick.stop_calls == 2
    assert brick.events == ("start", "stop")


@pytest.mark.asyncio
async def test_probe_service_lifecycle_exercises_start_health_and_stop() -> None:
    brick = FakeLifecycleBrick(name="scheduler")

    result = await probe_service_lifecycle(brick)

    assert result.name == "scheduler"
    assert result.started_after_start is True
    assert result.healthy_after_start is True
    assert result.started_after_stop is False
    assert result.healthy_after_stop is False
    assert result.events == ("start", "stop")


@pytest.mark.asyncio
async def test_probe_service_lifecycle_stops_after_health_failure() -> None:
    class _FailingHealthBrick(FakeLifecycleBrick):
        async def health_check(self) -> bool:
            raise RuntimeError("health failed")

    brick = _FailingHealthBrick(name="scheduler")

    with pytest.raises(RuntimeError, match="health failed"):
        await probe_service_lifecycle(brick)

    assert brick.started is False
    assert brick.events == ("start", "stop")


@pytest.mark.asyncio
async def test_fake_search_brick_satisfies_search_protocol_and_records_calls() -> None:
    brick = FakeSearchBrick(results=[{"path": "/docs/a.txt"}, {"path": "/docs/b.txt"}])

    assert isinstance(brick, SearchBrickProtocol)

    await brick.startup()
    results = await brick.search("docs", limit=1, zone_id="zone-a")
    await brick.notify_file_change("/docs/a.txt", "update")

    assert results == [{"path": "/docs/a.txt"}]
    assert brick.queries == (
        {
            "query": "docs",
            "search_type": "hybrid",
            "limit": 1,
            "path_filter": None,
            "alpha": 0.5,
            "fusion_method": "rrf",
            "adaptive_k": False,
            "zone_id": "zone-a",
        },
    )
    assert brick.notifications == (("/docs/a.txt", "update"),)
    assert brick.get_stats()["queries"] == 1
    assert brick.get_health()["status"] == "healthy"

    await brick.shutdown()
    assert brick.is_initialized is False
