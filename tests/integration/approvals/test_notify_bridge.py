"""LISTEN/NOTIFY bridge integration test."""

import asyncio
import json
import uuid

import pytest

from nexus.bricks.approvals.events import NotifyBridge

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_notify_payload_round_trip(asyncpg_pool):
    received: list[dict] = []
    event = asyncio.Event()
    channel = f"approvals_test_{uuid.uuid4().hex[:8]}"

    async def on_decided(payload: str) -> None:
        received.append(json.loads(payload))
        event.set()

    bridge = NotifyBridge(asyncpg_pool)
    await bridge.start({channel: on_decided})
    try:
        await bridge.notify(channel, json.dumps({"request_id": "rx", "decision": "approved"}))
        await asyncio.wait_for(event.wait(), 2.0)
    finally:
        await bridge.stop()

    assert received == [{"request_id": "rx", "decision": "approved"}]
