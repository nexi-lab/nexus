"""Unit tests for the Redis metrics hook added for `nexus hub status` (#3784)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as _redis_async

from nexus.bricks.mcp import metrics as hub_metrics
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
async def test_record_metrics_increments_per_zone_counters(monkeypatch):
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379")

    fake_client = AsyncMock()
    fake_client.incr = AsyncMock(return_value=1)
    fake_client.sadd = AsyncMock(return_value=1)
    fake_client.expire = AsyncMock(return_value=True)
    fake_client.close = AsyncMock()

    monkeypatch.setattr(_redis_async, "from_url", lambda _url: fake_client)

    await mw._record_metrics({"subject_id": "kid_abc", "token_hash": "deadbeef", "zone_id": "eng"})

    assert fake_client.incr.await_count == 2
    assert fake_client.sadd.await_count == 2

    incr_keys = [call.args[0] for call in fake_client.incr.await_args_list]
    sadd_keys = [call.args[0] for call in fake_client.sadd.await_args_list]
    expire_keys = [call.args[0] for call in fake_client.expire.await_args_list]

    assert sum(1 for key in incr_keys if re.fullmatch(r"nexus:hub:qps:\d+", key)) == 1
    assert sum(1 for key in incr_keys if re.fullmatch(r"nexus:hub:qps:zone:eng:\d+", key)) == 1
    assert sum(1 for key in sadd_keys if re.fullmatch(r"nexus:hub:active:\d+", key)) == 1
    assert sum(1 for key in sadd_keys if re.fullmatch(r"nexus:hub:active:zone:eng:\d+", key)) == 1
    assert fake_client.expire.await_count == 4
    assert set(expire_keys) == set(incr_keys + sadd_keys)


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


@pytest.mark.asyncio
async def test_record_metrics_updates_prometheus_without_redis(monkeypatch):
    monkeypatch.delenv("NEXUS_REDIS_URL", raising=False)
    monkeypatch.delenv("DRAGONFLY_URL", raising=False)
    hub_metrics._reset_for_tests()

    await mw._record_metrics(
        {
            "subject_id": "kid_abc",
            "token_hash": "deadbeef",
            "rpc_method": "tools/call",
            "tool_name": "nexus_grep",
            "status_code": 200,
            "latency_ms": 125,
        }
    )

    body = hub_metrics.render_metrics().decode()
    assert "nexus_mcp_requests_total" in body
    assert "nexus_mcp_request_latency_seconds_bucket" in body
    assert 'rpc_method="tools/call"' in body
    assert 'tool_name="nexus_grep"' in body
    assert 'status="2xx"' in body
    assert "nexus_mcp_active_clients 1.0" in body


@pytest.mark.asyncio
async def test_record_metrics_updates_prometheus_error_counter(monkeypatch):
    monkeypatch.delenv("NEXUS_REDIS_URL", raising=False)
    monkeypatch.delenv("DRAGONFLY_URL", raising=False)
    hub_metrics._reset_for_tests()

    await mw._record_metrics(
        {
            "subject_id": "kid_abc",
            "rpc_method": "tools/call",
            "tool_name": "nexus_write_file",
            "status_code": 500,
            "latency_ms": 7,
        }
    )

    body = hub_metrics.render_metrics().decode()
    assert "nexus_mcp_errors_total" in body
    assert 'status="5xx"' in body
