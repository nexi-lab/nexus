"""The distributed-lease invalidator honours the coordinator callback contract (Issue #4739).

``CacheCoordinator.register_lease_invalidator`` calls back with
``(zone_id, subject, relation, object)``.  The lifespan closure used to accept
only ``zone_id``; every notification raised ``TypeError`` inside the
coordinator (logged and swallowed), so distributed leases were never revoked.
It surfaced once ``rebac_delete`` started notifying lease invalidators.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.server.lifespan import permissions


class _FakeRedis:
    """Stands in for redis.asyncio.Redis: two leases in zone root, delete records."""

    def __init__(self, **_kwargs: Any) -> None:
        self.deleted: list[str] = []
        self.scans: list[str] = []

    async def scan_iter(self, match: str, count: int = 100):  # noqa: ARG002
        self.scans.append(match)
        for key in ("lease:root:agent-a:/ws", "lease:root:agent-b:/ws"):
            yield key

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


class _FakeStream:
    def __init__(self, **_kwargs: Any) -> None:
        self.handlers: dict[str, Any] = {}
        self.started = False

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    async def start(self) -> None:
        self.started = True


class _FakeLeaseManager:
    def __init__(self, *, redis_client: Any, zone_id: str) -> None:
        self._client = redis_client
        self.zone_id = zone_id


class _FakeCoordinator:
    def __init__(self) -> None:
        self.lease_invalidators: dict[str, Any] = {}
        self.durable_stream = None
        self.read_fence = None

    def register_lease_invalidator(self, callback_id: str, callback: Any) -> None:
        self.lease_invalidators[callback_id] = callback

    def set_durable_stream(self, stream: Any) -> None:
        self.durable_stream = stream

    def set_read_fence(self, fence: Any) -> None:
        self.read_fence = fence


@pytest.fixture
def wired(monkeypatch):
    fake_redis_instances: list[_FakeRedis] = []

    def _make_redis(**kwargs: Any) -> _FakeRedis:
        inst = _FakeRedis(**kwargs)
        fake_redis_instances.append(inst)
        return inst

    monkeypatch.setattr("redis.asyncio.Redis", _make_redis)
    monkeypatch.setattr(
        "nexus.bricks.rebac.cache.durable_stream.DurableInvalidationStream", _FakeStream
    )
    monkeypatch.setattr("nexus.lib.distributed_lease.DistributedLeaseManager", _FakeLeaseManager)

    coordinator = _FakeCoordinator()
    rebac = SimpleNamespace(_cache_coordinator=coordinator, _l1_cache=None)
    cache_store = SimpleNamespace(_client=SimpleNamespace(_pool=object()))
    app = SimpleNamespace(
        state=SimpleNamespace(
            cache_brick=SimpleNamespace(has_cache_store=True, cache_store=cache_store)
        )
    )
    svc = SimpleNamespace(rebac_manager=rebac, nexus_fs=None)
    return app, svc, coordinator, fake_redis_instances


@pytest.mark.asyncio
async def test_distributed_lease_invalidator_accepts_coordinator_contract(wired):
    app, svc, coordinator, redis_instances = wired

    await permissions._startup_durable_invalidation(app, svc)

    assert "distributed_lease" in coordinator.lease_invalidators
    callback = coordinator.lease_invalidators["distributed_lease"]

    # Exactly what CacheCoordinator._notify_lease_invalidators passes.
    callback("root", ("user", "bob"), "direct_viewer", ("file", "/ws/doc.txt"))

    # Fire-and-forget task: let it run to completion.
    for _ in range(5):
        await asyncio.sleep(0)

    assert redis_instances, "redis client was never constructed"
    client = redis_instances[-1]
    assert client.scans == ["lease:root:*"]
    assert client.deleted == ["lease:root:agent-a:/ws", "lease:root:agent-b:/ws"]


@pytest.mark.asyncio
async def test_startup_wires_stream_and_fence_into_coordinator(wired):
    app, svc, coordinator, _ = wired

    await permissions._startup_durable_invalidation(app, svc)

    assert isinstance(coordinator.durable_stream, _FakeStream)
    assert coordinator.durable_stream.started is True
    assert "local-cache-invalidate" in coordinator.durable_stream.handlers
    assert coordinator.read_fence is not None
    assert getattr(app.state, "distributed_lease_manager", None) is not None
