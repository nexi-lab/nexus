"""Audit-log integration test for MCP HTTP (#3779, AC 5).

Subscribes to the ``nexus:audit:mcp`` Redis Pub/Sub channel, drives one
real MCP tool call via the raw streamable-HTTP helper, and asserts a
structured record arrives with the expected fields populated.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from .conftest import mcp_http_call

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_audit_published_to_redis(mcp_http_base_url: str, seeded_zones) -> None:
    import redis.asyncio as redis

    redis_url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    assert redis_url, "NEXUS_REDIS_URL or DRAGONFLY_URL must be set"

    zone = seeded_zones[0]
    token = zone["api_key"]

    subscriber = redis.from_url(redis_url)
    pubsub = subscriber.pubsub()
    await pubsub.subscribe("nexus:audit:mcp")

    try:
        # Drain subscribe-confirmation / residual messages.
        for _ in range(5):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg is None:
                break

        await mcp_http_call(
            mcp_http_base_url,
            token,
            "tools/call",
            {"name": "nexus_glob", "arguments": {"pattern": "*.txt"}},
            timeout=30.0,
        )

        # Poll up to 5s for a tools/call audit record.
        seen: list[dict] = []
        tools_call_record: dict | None = None
        for _ in range(50):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg and msg["type"] == "message":
                rec = json.loads(msg["data"])
                seen.append(rec)
                if rec.get("rpc_method") == "tools/call":
                    tools_call_record = rec
                    break
            await asyncio.sleep(0.1)

        assert tools_call_record is not None, (
            f"no tools/call audit record observed. saw rpc_methods: "
            f"{[r.get('rpc_method') for r in seen]}"
        )
        assert tools_call_record["event"] == "mcp.request"
        assert tools_call_record["tool_name"] == "nexus_glob"
        assert tools_call_record["status_code"] in (200, 202)
        assert tools_call_record["token_hash"], "token_hash missing"
        # zone_id: populated once AuthIdentityCache has resolved the token.
        zone_id = tools_call_record.get("zone_id")
        assert zone_id in (None, zone["zone_id"]), (
            f"audit zone_id {zone_id!r} ≠ seeded {zone['zone_id']!r}"
        )
    finally:
        await pubsub.unsubscribe("nexus:audit:mcp")
        await pubsub.aclose()
        await subscriber.aclose()
