"""Regression tests for Issue #4338 — durable consumer vs. cache flush.

Prod incident: the ReBAC durable stream lives in a volumeless Dragonfly.
A Railway region move restarted it empty; publishers' XADD re-created the
stream but never the consumer group, so the consumer crash-looped on
NOGROUP forever instead of re-creating the group.

These tests pin the recovery contract with a purpose-built fake that
mirrors real Redis error semantics (NOGROUP / BUSYGROUP / MKSTREAM):

1. start() on an empty cache creates stream+group (XGROUP CREATE MKSTREAM)
2. group creation is idempotent across restarts (BUSYGROUP swallowed)
3. a mid-run flush self-heals: the consumer re-creates the group and
   resumes delivery

Related: Issue #4338, Issue #3396 (original stream design)
"""

import asyncio
import json

import pytest

from nexus.bricks.rebac.cache.channel_codec import encode_channel
from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
from nexus.bricks.rebac.cache.read_fence import ReadFence


class FakeResponseError(Exception):
    """Stands in for redis.exceptions.ResponseError (string-matched by prod code)."""


class FakeRedisStreams:
    """Minimal async Redis Streams fake with real error semantics.

    - XREADGROUP raises NOGROUP when the stream key or consumer group is
      missing (exact prod error shape)
    - XGROUP CREATE raises BUSYGROUP when the group exists, and ERR when
      the key is missing without MKSTREAM
    - flush() simulates a volumeless Dragonfly restart
    """

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._groups: dict[str, str] = {}  # "stream:group" -> last delivered id
        self._seq = 0

    def flush(self):
        """Simulate a volumeless Dragonfly restart: every key is gone."""
        self._streams.clear()
        self._groups.clear()

    async def xadd(self, name, fields, maxlen=None, approximate=False):
        self._seq += 1
        msg_id = f"{self._seq}-0"
        self._streams.setdefault(name, []).append((msg_id, fields))
        return msg_id

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        results = []
        for stream_key, _last_id in streams.items():
            group_key = f"{stream_key}:{groupname}"
            if group_key not in self._groups:
                raise FakeResponseError(
                    f"NOGROUP No such key '{stream_key}' or consumer group "
                    f"'{groupname}' in XREADGROUP with GROUP option"
                )
            if stream_key not in self._streams:
                continue
            offset = int(self._groups[group_key].split("-")[0])
            messages = [
                (mid, fields)
                for mid, fields in self._streams[stream_key]
                if int(mid.split("-")[0]) > offset
            ][:count]
            if messages:
                results.append((stream_key, messages))
                self._groups[group_key] = messages[-1][0]
        if results:
            return results
        if block:
            # Real XREADGROUP BLOCK suspends; yield so the consumer task
            # doesn't starve the test event loop.
            await asyncio.sleep(0.01)
        return None

    async def xack(self, name, groupname, *ids):
        return len(ids)

    async def xgroup_create(self, name, groupname, id="0", mkstream=False):
        group_key = f"{name}:{groupname}"
        if group_key in self._groups:
            raise FakeResponseError("BUSYGROUP Consumer Group name already exists")
        if name not in self._streams:
            if not mkstream:
                raise FakeResponseError(
                    "ERR The XGROUP subcommand requires the key to exist. Note that "
                    "for CREATE you may want to use the MKSTREAM option to create "
                    "an empty stream automatically."
                )
            self._streams[name] = []
        self._groups[group_key] = id

    async def xautoclaim(
        self, name, groupname, consumername, min_idle_time=0, start_id="0-0", count=10
    ):
        group_key = f"{name}:{groupname}"
        if group_key not in self._groups:
            raise FakeResponseError(f"NOGROUP No such key '{name}' or consumer group '{groupname}'")
        return ["0-0", [], []]


@pytest.fixture
def fake_redis():
    return FakeRedisStreams()


@pytest.mark.asyncio
async def test_start_on_empty_cache_creates_stream_and_group(fake_redis):
    """Empty cache at boot is normal: start() runs XGROUP CREATE MKSTREAM."""
    stream = DurableInvalidationStream(redis_client=fake_redis, zone_id="zone-b")
    await stream.start()
    try:
        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        assert stream_key in fake_redis._streams
        assert f"{stream_key}:zone:zone-b" in fake_redis._groups
    finally:
        await stream.stop()


@pytest.mark.asyncio
async def test_group_create_idempotent_on_restart(fake_redis):
    """BUSYGROUP from an existing group is swallowed (normal restart path)."""
    stream = DurableInvalidationStream(redis_client=fake_redis, zone_id="zone-b")
    await stream._ensure_consumer_groups()
    # Second create must not raise despite BUSYGROUP from the server
    await stream._ensure_consumer_groups()


@pytest.mark.asyncio
async def test_consumer_self_heals_after_cache_flush(fake_redis):
    """Mid-run cache flush: consumer re-creates the group and resumes.

    Reproduces the #4338 crash loop: group exists at start(), Dragonfly
    restarts empty, a publisher's XADD re-creates the stream but not the
    group, and every XREADGROUP fails with NOGROUP until the consumer
    re-creates the group itself.
    """
    fence = ReadFence()
    stream = DurableInvalidationStream(
        redis_client=fake_redis,
        zone_id="zone-b",
        read_fence=fence,
        consumer_block_ms=50,
    )

    received = asyncio.Event()

    async def handler(zone_id, payload):
        received.set()

    stream.register_handler("test", handler)
    await stream.start()
    try:
        # Let the consumer complete at least one healthy read cycle
        await asyncio.sleep(0.05)

        # Volumeless Dragonfly restarts empty
        fake_redis.flush()

        # A publisher in another zone keeps writing — XADD re-creates the
        # stream but never the consumer group
        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        await fake_redis.xadd(
            stream_key,
            {"data": json.dumps({"source_zone": "zone-a", "subject_id": "alice"})},
        )

        # Consumer must re-create the group and deliver the event
        await asyncio.wait_for(received.wait(), timeout=3.0)

        assert stream.stats()["group_recreates"] >= 1
        assert fence.watermark("zone-a") == 1
    finally:
        await stream.stop()


@pytest.mark.asyncio
async def test_recreate_failure_backs_off_then_recovers(fake_redis):
    """Half-up Redis: failed re-create takes the 1s backoff path, then heals.

    If XGROUP CREATE itself fails after a flush (e.g. Redis still coming
    up), the consumer must fall back to the error backoff instead of
    hot-looping, and recover once the server accepts the create.
    """
    fence = ReadFence()
    stream = DurableInvalidationStream(
        redis_client=fake_redis,
        zone_id="zone-b",
        read_fence=fence,
        consumer_block_ms=50,
    )

    received = asyncio.Event()

    async def handler(zone_id, payload):
        received.set()

    stream.register_handler("test", handler)
    await stream.start()
    try:
        await asyncio.sleep(0.05)
        fake_redis.flush()

        real_create = fake_redis.xgroup_create
        fail_remaining = 2

        async def flaky_create(*args, **kwargs):
            nonlocal fail_remaining
            if fail_remaining > 0:
                fail_remaining -= 1
                raise FakeResponseError("connection refused")
            return await real_create(*args, **kwargs)

        fake_redis.xgroup_create = flaky_create

        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        await fake_redis.xadd(
            stream_key,
            {"data": json.dumps({"source_zone": "zone-a", "subject_id": "alice"})},
        )

        # Two failed re-creates (~1s backoff each), then success + delivery
        await asyncio.wait_for(received.wait(), timeout=8.0)

        stats = stream.stats()
        assert stats["group_recreates"] >= 1
        assert stats["consume_errors"] >= 2  # the failed re-creates backed off
    finally:
        # Restore before stop() so a consumer mid-recreate at teardown
        # can't hit the flaky raise path and log spurious warnings.
        fake_redis.xgroup_create = real_create
        await stream.stop()
