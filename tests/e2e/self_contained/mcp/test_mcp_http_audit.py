"""Audit-log integration test for MCP HTTP (#3779, AC 5)."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded():
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")


@pytest.mark.asyncio
async def test_audit_published_to_redis(mcp_http_base_url: str) -> None:
    import redis.asyncio as redis

    redis_url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    assert redis_url, "NEXUS_REDIS_URL must be set for this test"

    subscriber = redis.from_url(redis_url)
    pubsub = subscriber.pubsub()
    await pubsub.subscribe("nexus:audit:mcp")

    try:
        # Drain any pre-existing messages.
        for _ in range(5):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg is None:
                break

        token = "sk-zone01_u_k_" + "a" * 32
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
                },
                timeout=10.0,
            )
            assert resp.status_code == 200

        # Wait up to 5s for the publish task to land.
        record = None
        for _ in range(50):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg and msg["type"] == "message":
                record = json.loads(msg["data"])
                break
            await asyncio.sleep(0.1)

        assert record is not None, "no audit record published"
        assert record["event"] == "mcp.request"
        assert record["rpc_method"] == "tools/call"
        assert record["tool_name"] == "nexus_grep"
        assert record["status_code"] == 200
        assert record["zone_id"] == "zone-01"
        assert record["token_hash"] is not None
    finally:
        await pubsub.unsubscribe("nexus:audit:mcp")
        await pubsub.aclose()
        await subscriber.aclose()
