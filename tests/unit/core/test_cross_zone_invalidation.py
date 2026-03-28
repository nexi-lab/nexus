"""Cross-zone cache invalidation coherence tests.

Simulates a 2-zone topology with mock Dragonfly to verify:
1. Zone A grants permission → publishes to durable stream
2. Zone B's consumer processes event → read fence watermark advances
3. Zone B's cache is correctly detected as stale

No Docker required — uses in-process mocking.

Related: Issue #3396 (decision 10A)
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
from nexus.bricks.rebac.cache.coordinator_config import (
    CoordinatorConfig,
    InvalidationChannels,
)
from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
from nexus.bricks.rebac.cache.read_fence import ReadFence

pytest.importorskip("pyroaring")


class MockRedisStream:
    """In-memory mock of async Redis client supporting Streams API."""

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._groups: dict[str, dict[str, str]] = {}  # stream -> {group: last_id}
        self._seq = 0

    async def xadd(self, name, fields, maxlen=None, approximate=False):
        self._seq += 1
        msg_id = f"{self._seq}-0"
        if name not in self._streams:
            self._streams[name] = []
        self._streams[name].append((msg_id, fields))
        return msg_id

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        results = []
        for stream_key, _last_id in streams.items():
            if stream_key not in self._streams:
                continue
            group_key = f"{stream_key}:{groupname}"
            offset = int(self._groups.get(group_key, "0").split("-")[0])
            messages = [
                (mid, fields)
                for mid, fields in self._streams[stream_key]
                if int(mid.split("-")[0]) > offset
            ][:count]
            if messages:
                results.append((stream_key, messages))
                # Advance group offset
                self._groups[group_key] = messages[-1][0]
        return results if results else None

    async def xack(self, name, groupname, *ids):
        return len(ids)

    async def xgroup_create(self, name, groupname, id="0", mkstream=False):
        if mkstream and name not in self._streams:
            self._streams[name] = []
        group_key = f"{name}:{groupname}"
        self._groups[group_key] = id

    def pipeline(self, transaction=False):
        return MockPipeline(self)


class MockPipeline:
    """Mock Redis pipeline that collects commands and executes sequentially."""

    def __init__(self, client):
        self._client = client
        self._commands = []

    def xadd(self, name, fields, maxlen=None, approximate=False):
        self._commands.append(("xadd", name, fields, maxlen))
        return self

    async def execute(self):
        results = []
        for cmd in self._commands:
            if cmd[0] == "xadd":
                result = await self._client.xadd(cmd[1], cmd[2], maxlen=cmd[3])
                results.append(result)
        self._commands.clear()
        return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrossZoneInvalidation:
    """Simulated 2-zone topology tests."""

    @pytest.fixture
    def mock_redis(self):
        return MockRedisStream()

    @pytest.fixture
    def fence_a(self):
        return ReadFence()

    @pytest.fixture
    def fence_b(self):
        return ReadFence()

    @pytest.fixture
    def zone_a_stream(self, mock_redis, fence_a):
        return DurableInvalidationStream(
            redis_client=mock_redis,
            zone_id="zone-a",
            read_fence=fence_a,
        )

    @pytest.fixture
    def zone_b_stream(self, mock_redis, fence_b):
        return DurableInvalidationStream(
            redis_client=mock_redis,
            zone_id="zone-b",
            read_fence=fence_b,
        )

    def test_zone_a_publish_enqueues_event(self, zone_a_stream):
        """Zone A publishing enqueues an event in the local deque."""
        result = zone_a_stream.publish(
            "zone-b",
            {"subject_type": "user", "subject_id": "alice", "relation": "editor"},
        )
        assert result is True
        assert zone_a_stream.stats()["published"] == 1
        assert zone_a_stream.stats()["queue_size"] == 1

    @pytest.mark.asyncio
    async def test_zone_a_drain_writes_to_redis_stream(self, zone_a_stream, mock_redis):
        """Draining Zone A's queue writes events to Redis Streams."""
        zone_a_stream.publish(
            "zone-b",
            {"subject_type": "user", "subject_id": "alice", "relation": "editor"},
        )

        await zone_a_stream._drain_batch()

        assert zone_a_stream.stats()["drained"] == 1
        assert zone_a_stream.stats()["queue_size"] == 0
        # Verify Redis stream has the event
        from nexus.bricks.rebac.cache.channel_codec import encode_channel

        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        assert len(mock_redis._streams.get(stream_key, [])) == 1

    @pytest.mark.asyncio
    async def test_zone_b_consumer_processes_event_and_advances_fence(
        self, zone_a_stream, zone_b_stream, mock_redis, fence_b
    ):
        """Zone B consumer reads Zone A's event and advances the read fence."""
        # Zone A publishes an invalidation
        zone_a_stream.publish(
            "zone-b",
            {
                "source_zone": "zone-a",
                "subject_type": "user",
                "subject_id": "alice",
                "relation": "editor",
                "object_type": "file",
                "object_id": "/doc.txt",
            },
        )
        await zone_a_stream._drain_batch()

        # Set up consumer group for Zone B
        from nexus.bricks.rebac.cache.channel_codec import encode_channel

        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        await mock_redis.xgroup_create(stream_key, "zone:zone-b", id="0")

        # Register a handler on Zone B
        handler_calls = []

        async def test_handler(zone_id, payload):
            handler_calls.append((zone_id, payload))

        zone_b_stream.register_handler("test", test_handler)

        # Manually read and process (simulating one iteration of consume loop)
        results = await mock_redis.xreadgroup(
            groupname="zone:zone-b",
            consumername="zone-b:consumer",
            streams={stream_key: ">"},
            count=10,
        )

        assert results is not None
        # Process messages
        sem = asyncio.Semaphore(10)
        for _stream, messages in results:
            for msg_id, fields in messages:
                await zone_b_stream._process_message(sem, stream_key, msg_id, fields)

        # Handler was called
        assert len(handler_calls) == 1
        assert handler_calls[0][0] == "zone-a"
        assert handler_calls[0][1]["subject_id"] == "alice"

        # Read fence was advanced
        assert fence_b.watermark("zone-a") > 0

    def test_read_fence_detects_stale_cache(self, fence_b):
        """A cached result from before the watermark is detected as stale."""
        # Cache was populated at sequence 5
        cached_sequence = 5

        # No revocation yet — cache is fresh
        assert fence_b.is_stale("zone-a", cached_sequence) is False

        # Zone B receives a revocation at sequence 10
        fence_b.advance("zone-a", 10)

        # Now the cache is stale
        assert fence_b.is_stale("zone-a", cached_sequence) is True
        # A fresh cache entry at sequence 10 or later is not stale
        assert fence_b.is_stale("zone-a", 10) is False
        assert fence_b.is_stale("zone-a", 15) is False

    def test_read_fence_independent_per_zone(self, fence_b):
        """Watermarks are tracked independently per zone."""
        fence_b.advance("zone-a", 10)
        fence_b.advance("zone-c", 5)

        # zone-a watermark doesn't affect zone-c check
        assert fence_b.is_stale("zone-a", 7) is True
        assert fence_b.is_stale("zone-c", 7) is False
        assert fence_b.is_stale("zone-c", 3) is True

    def test_coordinator_publishes_to_durable_stream(self):
        """CacheCoordinator.invalidate_for_write() publishes to durable stream."""
        mock_durable = MagicMock()
        mock_durable.publish = MagicMock(return_value=True)
        fence = ReadFence()

        config = CoordinatorConfig(
            channels=InvalidationChannels(
                durable_stream=mock_durable,
                read_fence=fence,
            )
        )

        l1 = MagicMock()
        coordinator = CacheCoordinator(
            l1_cache=l1,
            zone_graph_cache={"zone-a": {}},
            config=config,
        )

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        mock_durable.publish.assert_called_once()
        call_args = mock_durable.publish.call_args
        assert call_args.kwargs["target_zone_id"] == "zone-a"
        payload = call_args.kwargs["payload"]
        assert payload["source_zone"] == "zone-a"
        assert payload["subject_id"] == "alice"
        assert payload["relation"] == "editor"

    @pytest.mark.asyncio
    async def test_full_cross_zone_flow(self, mock_redis):
        """End-to-end: Zone A write → durable stream → Zone B fence updated."""
        fence_a = ReadFence()
        fence_b = ReadFence()

        stream_a = DurableInvalidationStream(
            redis_client=mock_redis,
            zone_id="zone-a",
            read_fence=fence_a,
        )
        stream_b = DurableInvalidationStream(
            redis_client=mock_redis,
            zone_id="zone-b",
            read_fence=fence_b,
        )

        # Zone A coordinator publishes
        config_a = CoordinatorConfig(
            channels=InvalidationChannels(
                durable_stream=stream_a,
                read_fence=fence_a,
            )
        )
        coord_a = CacheCoordinator(
            l1_cache=MagicMock(),
            zone_graph_cache={"zone-b": {}},
            config=config_a,
        )

        # Zone B has a handler that invalidates its local cache
        zone_b_invalidated = []

        async def zone_b_handler(zone_id, payload):
            zone_b_invalidated.append(payload)

        stream_b.register_handler("cache-invalidate", zone_b_handler)

        # Step 1: Zone A writes a permission
        coord_a.invalidate_for_write(
            zone_id="zone-b",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        # Step 2: Drain Zone A's queue to Redis
        await stream_a._drain_batch()

        # Step 3: Create consumer group for Zone B
        from nexus.bricks.rebac.cache.channel_codec import encode_channel

        stream_key = encode_channel("rebac:durable", "zone-b", "all")
        await mock_redis.xgroup_create(stream_key, "zone:zone-b", id="0")

        # Step 4: Zone B consumes
        results = await mock_redis.xreadgroup(
            groupname="zone:zone-b",
            consumername="zone-b:consumer",
            streams={stream_key: ">"},
            count=10,
        )
        assert results is not None

        sem = asyncio.Semaphore(10)
        for _stream, messages in results:
            for msg_id, fields in messages:
                await stream_b._process_message(sem, stream_key, msg_id, fields)

        # Verify: Zone B handler was called
        assert len(zone_b_invalidated) == 1
        assert zone_b_invalidated[0]["subject_id"] == "alice"

        # Verify: Zone B's read fence was advanced
        assert fence_b.watermark("zone-b") > 0

        # Verify: A cached result from before the watermark is stale
        assert fence_b.is_stale("zone-b", 0) is True
