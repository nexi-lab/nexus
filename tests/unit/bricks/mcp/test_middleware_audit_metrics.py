"""Unit tests for the Redis metrics hook added for `nexus hub status` (#3784)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import redis.asyncio as _redis_async

from nexus.bricks.mcp import middleware_audit as mw


@pytest.mark.asyncio
async def test_record_metrics_increments_qps_and_sadds_active(monkeypatch):
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379")

    fake_client = AsyncMock()
    fake_client.incr = AsyncMock(return_value=1)
    fake_client.sadd = AsyncMock(return_value=1)
    fake_client.expire = AsyncMock(return_value=True)
    fake_client.close = AsyncMock()

    monkeypatch.setattr(_redis_async, "from_url", lambda _url: fake_client)

    await mw._record_metrics({"subject_id": "kid_abc", "token_hash": "deadbeef"})

    fake_client.incr.assert_awaited_once()
    qps_key = fake_client.incr.await_args.args[0]
    assert qps_key.startswith("nexus:hub:qps:")

    fake_client.sadd.assert_awaited_once()
    active_key, member = fake_client.sadd.await_args.args
    assert active_key.startswith("nexus:hub:active:")
    assert member == "kid_abc"  # subject_id is what identifies a client

    assert fake_client.expire.await_count == 2


@pytest.mark.asyncio
async def test_record_metrics_no_redis_url_is_noop(monkeypatch):
    monkeypatch.delenv("NEXUS_REDIS_URL", raising=False)
    monkeypatch.delenv("DRAGONFLY_URL", raising=False)
    await mw._record_metrics({"subject_id": "kid_x"})


@pytest.mark.asyncio
async def test_record_metrics_swallows_redis_errors(monkeypatch):
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379")

    fake_client = AsyncMock()
    fake_client.incr = AsyncMock(side_effect=RuntimeError("boom"))
    fake_client.close = AsyncMock()

    monkeypatch.setattr(_redis_async, "from_url", lambda _url: fake_client)

    # Must not raise — audit is fire-and-forget.
    await mw._record_metrics({"subject_id": "kid_abc"})
