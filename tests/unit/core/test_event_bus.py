"""Unit tests for distributed event bus.

Tests cover:
- FileEvent dataclass and serialization
- FileEventType enum
- Path pattern matching
- RedisEventBus (mocked Redis)
- Factory functions

Related: Issue #1106 Block 2
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.event_bus import (
    EventBusBase,
    EventBusProtocol,
    FileEvent,
    FileEventType,
    GlobalEventBus,
    RedisEventBus,
    create_event_bus,
    get_global_event_bus,
    set_global_event_bus,
)

# =============================================================================
# FileEventType Tests
# =============================================================================


class TestFileEventType:
    """Tests for FileEventType enum."""

    def test_event_types_defined(self):
        """Test that all expected event types are defined."""
        assert FileEventType.FILE_WRITE == "file_write"
        assert FileEventType.FILE_DELETE == "file_delete"
        assert FileEventType.FILE_RENAME == "file_rename"
        assert FileEventType.DIR_CREATE == "dir_create"
        assert FileEventType.DIR_DELETE == "dir_delete"

    def test_event_type_string_value(self):
        """Test that event types are strings."""
        assert isinstance(FileEventType.FILE_WRITE.value, str)
        assert FileEventType.FILE_WRITE.value == "file_write"


# =============================================================================
# FileEvent Tests
# =============================================================================


class TestFileEvent:
    """Tests for FileEvent dataclass."""

    def test_create_basic_event(self):
        """Test creating a basic FileEvent."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/inbox/test.txt"
        assert event.zone_id == "zone1"
        assert event.event_id is not None
        assert event.timestamp is not None

    def test_create_event_with_optional_fields(self):
        """Test creating event with all optional fields."""
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/inbox/new.txt",
            zone_id="zone1",
            old_path="/inbox/old.txt",
            size=1024,
            etag="abc123",
            agent_id="agent-1",
        )

        assert event.old_path == "/inbox/old.txt"
        assert event.size == 1024
        assert event.etag == "abc123"
        assert event.agent_id == "agent-1"

    def test_to_dict_basic(self):
        """Test serializing event to dictionary."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
            event_id="event-123",
        )

        data = event.to_dict()

        assert data["type"] == "file_write"
        assert data["path"] == "/inbox/test.txt"
        assert data["zone_id"] == "zone1"
        assert data["event_id"] == "event-123"
        assert "timestamp" in data

    def test_to_dict_with_optional_fields(self):
        """Test serializing event with optional fields."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
            old_path="/inbox/old.txt",
            size=1024,
        )

        data = event.to_dict()

        assert data["old_path"] == "/inbox/old.txt"
        assert data["size"] == 1024
        assert "etag" not in data  # None values not included
        assert "agent_id" not in data

    def test_to_json(self):
        """Test serializing event to JSON string."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        json_str = event.to_json()

        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert parsed["type"] == "file_write"
        assert parsed["path"] == "/inbox/test.txt"

    def test_from_dict(self):
        """Test creating event from dictionary."""
        data = {
            "type": "file_write",
            "path": "/inbox/test.txt",
            "zone_id": "zone1",
            "timestamp": "2024-01-01T00:00:00Z",
            "event_id": "event-123",
        }

        event = FileEvent.from_dict(data)

        assert event.type == "file_write"
        assert event.path == "/inbox/test.txt"
        assert event.zone_id == "zone1"
        assert event.event_id == "event-123"

    def test_from_json_string(self):
        """Test creating event from JSON string."""
        json_str = '{"type": "file_delete", "path": "/inbox/test.txt", "zone_id": "zone1"}'

        event = FileEvent.from_json(json_str)

        assert event.type == "file_delete"
        assert event.path == "/inbox/test.txt"

    def test_from_json_bytes(self):
        """Test creating event from JSON bytes."""
        json_bytes = b'{"type": "file_delete", "path": "/inbox/test.txt", "zone_id": "zone1"}'

        event = FileEvent.from_json(json_bytes)

        assert event.type == "file_delete"
        assert event.path == "/inbox/test.txt"

    def test_roundtrip_serialization(self):
        """Test event can be serialized and deserialized."""
        original = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/inbox/new.txt",
            zone_id="zone1",
            old_path="/inbox/old.txt",
            size=2048,
            etag="hash123",
        )

        json_str = original.to_json()
        restored = FileEvent.from_json(json_str)

        assert restored.type == "file_rename"  # Note: Enum becomes string
        assert restored.path == original.path
        assert restored.zone_id == original.zone_id
        assert restored.old_path == original.old_path
        assert restored.size == original.size
        assert restored.etag == original.etag

    def test_event_with_none_zone_id(self):
        """Test that zone_id can be None (Layer 1 local events)."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id=None,  # Layer 1 doesn't have zone
        )

        assert event.zone_id is None

        # to_dict should not include zone_id when None
        data = event.to_dict()
        assert "zone_id" not in data

    def test_from_dict_without_zone_id(self):
        """Test that from_dict handles missing zone_id."""
        data = {
            "type": "file_write",
            "path": "/inbox/test.txt",
            # No zone_id
        }

        event = FileEvent.from_dict(data)

        assert event.zone_id is None
        assert event.type == "file_write"
        assert event.path == "/inbox/test.txt"


class TestFileEventFromFileChange:
    """Tests for FileEvent.from_file_change() conversion from Layer 1."""

    def test_from_file_change_created(self):
        """Test converting CREATED FileChange to FILE_WRITE FileEvent."""
        from dataclasses import dataclass
        from enum import Enum

        class MockChangeType(Enum):
            CREATED = "created"

        @dataclass
        class MockFileChange:
            type: MockChangeType
            path: str
            old_path: str | None = None

        change = MockFileChange(type=MockChangeType.CREATED, path="new_file.txt")
        event = FileEvent.from_file_change(change)

        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "new_file.txt"
        assert event.zone_id is None
        assert event.event_id is not None

    def test_from_file_change_modified(self):
        """Test converting MODIFIED FileChange to FILE_WRITE FileEvent."""
        from dataclasses import dataclass
        from enum import Enum

        class MockChangeType(Enum):
            MODIFIED = "modified"

        @dataclass
        class MockFileChange:
            type: MockChangeType
            path: str
            old_path: str | None = None

        change = MockFileChange(type=MockChangeType.MODIFIED, path="changed_file.txt")
        event = FileEvent.from_file_change(change)

        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "changed_file.txt"

    def test_from_file_change_deleted(self):
        """Test converting DELETED FileChange to FILE_DELETE FileEvent."""
        from dataclasses import dataclass
        from enum import Enum

        class MockChangeType(Enum):
            DELETED = "deleted"

        @dataclass
        class MockFileChange:
            type: MockChangeType
            path: str
            old_path: str | None = None

        change = MockFileChange(type=MockChangeType.DELETED, path="deleted_file.txt")
        event = FileEvent.from_file_change(change)

        assert event.type == FileEventType.FILE_DELETE
        assert event.path == "deleted_file.txt"

    def test_from_file_change_renamed(self):
        """Test converting RENAMED FileChange to FILE_RENAME FileEvent."""
        from dataclasses import dataclass
        from enum import Enum

        class MockChangeType(Enum):
            RENAMED = "renamed"

        @dataclass
        class MockFileChange:
            type: MockChangeType
            path: str
            old_path: str | None = None

        change = MockFileChange(
            type=MockChangeType.RENAMED,
            path="new_name.txt",
            old_path="old_name.txt",
        )
        event = FileEvent.from_file_change(change)

        assert event.type == FileEventType.FILE_RENAME
        assert event.path == "new_name.txt"
        assert event.old_path == "old_name.txt"

    def test_from_file_change_with_zone_id(self):
        """Test converting FileChange with zone_id."""
        from dataclasses import dataclass
        from enum import Enum

        class MockChangeType(Enum):
            CREATED = "created"

        @dataclass
        class MockFileChange:
            type: MockChangeType
            path: str
            old_path: str | None = None

        change = MockFileChange(type=MockChangeType.CREATED, path="file.txt")
        event = FileEvent.from_file_change(change, zone_id="my-zone")

        assert event.zone_id == "my-zone"


class TestFileEventPathMatching:
    """Tests for FileEvent path pattern matching."""

    def test_exact_match(self):
        """Test exact path matching."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/inbox/test.txt") is True
        assert event.matches_path_pattern("/inbox/other.txt") is False

    def test_directory_match(self):
        """Test directory pattern matching."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/subdir/test.txt",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/inbox/") is True
        assert event.matches_path_pattern("/inbox/subdir/") is True
        assert event.matches_path_pattern("/other/") is False

    def test_directory_exact_match(self):
        """Test matching directory itself."""
        event = FileEvent(
            type=FileEventType.DIR_CREATE,
            path="/inbox",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/inbox/") is True
        assert event.matches_path_pattern("/inbox") is True

    def test_glob_star_pattern(self):
        """Test glob * pattern matching."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/inbox/*.txt") is True
        assert event.matches_path_pattern("/inbox/*.pdf") is False
        # Note: fnmatch's * matches any characters, so /*.txt matches /inbox/test.txt
        assert event.matches_path_pattern("/*.txt") is True  # fnmatch * matches slashes too

    def test_glob_question_pattern(self):
        """Test glob ? pattern matching."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/inbox/tes?.txt") is True
        assert event.matches_path_pattern("/inbox/test.tx?") is True
        assert event.matches_path_pattern("/inbox/t?st.txt") is True

    def test_glob_double_star_pattern(self):
        """Test glob ** pattern matching."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/deep/nested/test.txt",
            zone_id="zone1",
        )

        # fnmatch uses shell patterns, ** matches any characters
        assert event.matches_path_pattern("/inbox/**") is True
        assert event.matches_path_pattern("**/*.txt") is True

    def test_rename_matches_old_path(self):
        """Test that rename events match old_path."""
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/inbox/new.txt",
            zone_id="zone1",
            old_path="/inbox/old.txt",
        )

        # Matches new path
        assert event.matches_path_pattern("/inbox/new.txt") is True

        # Also matches old path
        assert event.matches_path_pattern("/inbox/old.txt") is True

        # Matches directory containing both
        assert event.matches_path_pattern("/inbox/") is True

    def test_rename_old_path_glob(self):
        """Test rename old_path with glob patterns."""
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/archive/file.txt",
            zone_id="zone1",
            old_path="/inbox/file.txt",
        )

        assert event.matches_path_pattern("/inbox/*.txt") is True
        assert event.matches_path_pattern("/archive/*.txt") is True
        assert event.matches_path_pattern("/other/*.txt") is False

    def test_no_match(self):
        """Test pattern that doesn't match."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        assert event.matches_path_pattern("/other/test.txt") is False
        assert event.matches_path_pattern("/inbox/other.txt") is False


# =============================================================================
# RedisEventBus Tests (Mocked)
# =============================================================================


@pytest.fixture
def mock_redis_client():
    """Create a mock DragonflyClient."""
    client = MagicMock()
    client.client = MagicMock()
    client.health_check = AsyncMock(return_value=True)
    client.get_info = AsyncMock(return_value={"status": "ok"})
    return client


@pytest.fixture
def mock_pubsub():
    """Create a mock Redis PubSub."""
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.close = AsyncMock()
    pubsub.aclose = AsyncMock()  # New async close method
    pubsub.get_message = AsyncMock(return_value=None)
    return pubsub


class TestRedisEventBus:
    """Tests for RedisEventBus with mocked Redis."""

    @pytest.mark.asyncio
    async def test_start_stop(self, mock_redis_client, mock_pubsub):
        """Test starting and stopping the event bus."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        bus = RedisEventBus(mock_redis_client)

        # Initially not started
        assert bus._started is False

        # Start
        await bus.start()
        assert bus._started is True
        mock_redis_client.client.pubsub.assert_called_once()

        # Double start is idempotent
        await bus.start()
        assert mock_redis_client.client.pubsub.call_count == 1

        # Stop
        await bus.stop()
        assert bus._started is False
        mock_pubsub.aclose.assert_called_once()

        # Double stop is idempotent
        await bus.stop()
        assert mock_pubsub.aclose.call_count == 1

    @pytest.mark.asyncio
    async def test_publish_requires_start(self, mock_redis_client):
        """Test that publish raises error if not started."""
        bus = RedisEventBus(mock_redis_client)
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="zone1",
        )

        with pytest.raises(RuntimeError, match="not started"):
            await bus.publish(event)

    @pytest.mark.asyncio
    async def test_publish_event(self, mock_redis_client, mock_pubsub):
        """Test publishing an event."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub
        mock_redis_client.client.publish = AsyncMock(return_value=2)

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        )

        num_subscribers = await bus.publish(event)

        assert num_subscribers == 2
        mock_redis_client.client.publish.assert_called_once()
        call_args = mock_redis_client.client.publish.call_args
        assert call_args[0][0] == "nexus:events:zone1"  # Channel
        assert "file_write" in call_args[0][1]  # Message contains event type

    @pytest.mark.asyncio
    async def test_channel_name(self, mock_redis_client):
        """Test channel name generation."""
        bus = RedisEventBus(mock_redis_client)

        assert bus._channel_name("zone1") == "nexus:events:zone1"
        assert bus._channel_name("default") == "nexus:events:default"
        assert bus._channel_name("multi-zone-123") == "nexus:events:multi-zone-123"

    @pytest.mark.asyncio
    async def test_wait_for_event_requires_start(self, mock_redis_client):
        """Test that wait_for_event raises error if not started."""
        bus = RedisEventBus(mock_redis_client)

        with pytest.raises(RuntimeError, match="not started"):
            await bus.wait_for_event("zone1", "/inbox/")

    @pytest.mark.asyncio
    async def test_wait_for_event_timeout(self, mock_redis_client, mock_pubsub):
        """Test wait_for_event returns None on timeout."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub
        mock_pubsub.get_message = AsyncMock(return_value=None)

        # Create a separate pubsub for wait
        wait_pubsub = MagicMock()
        wait_pubsub.subscribe = AsyncMock()
        wait_pubsub.unsubscribe = AsyncMock()
        wait_pubsub.close = AsyncMock()
        wait_pubsub.aclose = AsyncMock()
        wait_pubsub.get_message = AsyncMock(return_value=None)

        # First call returns mock_pubsub for start(), second returns wait_pubsub
        call_count = [0]

        def create_pubsub():
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_pubsub
            return wait_pubsub

        mock_redis_client.client.pubsub = create_pubsub

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.wait_for_event("zone1", "/inbox/", timeout=0.1)

        assert result is None

    @pytest.mark.asyncio
    async def test_wait_for_event_receives_matching_event(self, mock_redis_client, mock_pubsub):
        """Test wait_for_event returns matching event."""
        # Setup initial pubsub for start()
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        # Create wait pubsub that returns an event
        wait_pubsub = MagicMock()
        wait_pubsub.subscribe = AsyncMock()
        wait_pubsub.unsubscribe = AsyncMock()
        wait_pubsub.close = AsyncMock()
        wait_pubsub.aclose = AsyncMock()

        event_data = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        ).to_json()

        message = {
            "type": "message",
            "data": event_data.encode("utf-8"),
        }
        wait_pubsub.get_message = AsyncMock(return_value=message)

        # Track pubsub calls
        call_count = [0]

        def create_pubsub():
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_pubsub
            return wait_pubsub

        mock_redis_client.client.pubsub = create_pubsub

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.wait_for_event("zone1", "/inbox/", timeout=5.0)

        assert result is not None
        assert result.type == "file_write"
        assert result.path == "/inbox/test.txt"

    @pytest.mark.asyncio
    async def test_wait_for_event_ignores_non_matching(self, mock_redis_client, mock_pubsub):
        """Test wait_for_event ignores non-matching events."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        # Create wait pubsub that returns non-matching then matching event
        wait_pubsub = MagicMock()
        wait_pubsub.subscribe = AsyncMock()
        wait_pubsub.unsubscribe = AsyncMock()
        wait_pubsub.close = AsyncMock()
        wait_pubsub.aclose = AsyncMock()

        non_matching_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/other/test.txt",  # Not in /inbox/
            zone_id="zone1",
        ).to_json()

        matching_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="zone1",
        ).to_json()

        messages = [
            {"type": "message", "data": non_matching_event.encode()},
            {"type": "message", "data": matching_event.encode()},
        ]
        message_iter = iter(messages)
        wait_pubsub.get_message = AsyncMock(side_effect=lambda **kw: next(message_iter, None))

        call_count = [0]

        def create_pubsub():
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_pubsub
            return wait_pubsub

        mock_redis_client.client.pubsub = create_pubsub

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.wait_for_event("zone1", "/inbox/", timeout=5.0)

        assert result is not None
        assert result.path == "/inbox/test.txt"

    @pytest.mark.asyncio
    async def test_health_check_when_running(self, mock_redis_client, mock_pubsub):
        """Test health check when bus is running."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.health_check()

        assert result is True
        mock_redis_client.health_check.assert_called()

    @pytest.mark.asyncio
    async def test_health_check_when_not_started(self, mock_redis_client):
        """Test health check returns False when not started."""
        bus = RedisEventBus(mock_redis_client)

        result = await bus.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_get_stats(self, mock_redis_client, mock_pubsub):
        """Test getting event bus stats."""
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        stats = await bus.get_stats()

        assert stats["backend"] == "redis_pubsub"
        assert stats["status"] == "running"
        assert stats["channel_prefix"] == "nexus:events"


# =============================================================================
# Factory and Singleton Tests
# =============================================================================


class TestEventBusFactory:
    """Tests for event bus factory function."""

    def test_create_redis_event_bus(self, mock_redis_client):
        """Test creating Redis event bus via factory."""
        bus = create_event_bus(backend="redis", redis_client=mock_redis_client)

        assert isinstance(bus, RedisEventBus)
        assert isinstance(bus, EventBusBase)

    def test_create_redis_requires_client(self):
        """Test that Redis backend requires redis_client."""
        with pytest.raises(ValueError, match="redis_client is required"):
            create_event_bus(backend="redis")

    def test_unsupported_backend(self, mock_redis_client):
        """Test error for unsupported backend."""
        with pytest.raises(ValueError, match="Unsupported event bus backend"):
            create_event_bus(backend="unknown", redis_client=mock_redis_client)


class TestGlobalEventBusSingleton:
    """Tests for global event bus singleton management."""

    def test_get_set_global_event_bus(self, mock_redis_client):
        """Test setting and getting global event bus."""
        # Initially None
        set_global_event_bus(None)
        assert get_global_event_bus() is None

        # Set a bus
        bus = RedisEventBus(mock_redis_client)
        set_global_event_bus(bus)
        assert get_global_event_bus() is bus

        # Clear
        set_global_event_bus(None)
        assert get_global_event_bus() is None

    def test_global_event_bus_alias(self, mock_redis_client):
        """Test that GlobalEventBus is an alias for RedisEventBus."""
        assert GlobalEventBus is RedisEventBus

        bus = GlobalEventBus(mock_redis_client)
        assert isinstance(bus, RedisEventBus)


# =============================================================================
# Protocol Compliance Tests
# =============================================================================


class TestEventBusProtocol:
    """Tests for EventBusProtocol compliance."""

    def test_redis_event_bus_implements_protocol(self, mock_redis_client):
        """Test that RedisEventBus implements EventBusProtocol."""
        bus = RedisEventBus(mock_redis_client)

        # Check protocol compliance via runtime_checkable
        assert isinstance(bus, EventBusProtocol)

        # Check all required methods exist
        assert hasattr(bus, "start")
        assert hasattr(bus, "stop")
        assert hasattr(bus, "publish")
        assert hasattr(bus, "wait_for_event")
        assert hasattr(bus, "health_check")
        assert hasattr(bus, "subscribe")  # New subscribe method


# =============================================================================
# Subscribe Method Tests
# =============================================================================


class TestSubscribeMethod:
    """Tests for the subscribe() async generator method."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        client = MagicMock()
        client.client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_subscribe_yields_events(self, mock_redis_client):
        """Test that subscribe() yields FileEvent objects."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = True

        # Create mock pubsub
        mock_pubsub = AsyncMock()
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        # Prepare test events
        test_events = [
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/test/file1.txt",
                zone_id="zone-1",
            ),
            FileEvent(
                type=FileEventType.FILE_DELETE,
                path="/test/file2.txt",
                zone_id="zone-1",
            ),
        ]

        # Mock get_message to return events then None
        call_count = [0]

        async def mock_get_message(*args, **kwargs):
            if call_count[0] < len(test_events):
                event = test_events[call_count[0]]
                call_count[0] += 1
                return {"type": "message", "data": event.to_json()}
            return None

        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()

        # Collect events from subscribe generator
        received = []
        count = 0
        async for event in bus.subscribe("zone-1"):
            received.append(event)
            count += 1
            if count >= 2:
                break

        assert len(received) == 2
        assert received[0].path == "/test/file1.txt"
        assert received[1].path == "/test/file2.txt"

    @pytest.mark.asyncio
    async def test_subscribe_requires_started(self, mock_redis_client):
        """Test that subscribe() raises if bus not started."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = False

        with pytest.raises(RuntimeError, match="not started"):
            async for _ in bus.subscribe("zone-1"):
                pass

    @pytest.mark.asyncio
    async def test_subscribe_filters_non_message_types(self, mock_redis_client):
        """Test that subscribe() ignores non-message types."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = True

        mock_pubsub = AsyncMock()
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        # Create one real event
        test_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="zone-1",
        )

        # Mock get_message to return subscribe confirmation, then event, then None
        messages = [
            {"type": "subscribe", "data": 1},  # Should be ignored
            {"type": "message", "data": test_event.to_json()},  # Should be yielded
        ]
        call_count = [0]

        async def mock_get_message(*args, **kwargs):
            if call_count[0] < len(messages):
                msg = messages[call_count[0]]
                call_count[0] += 1
                return msg
            return None

        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()

        received = []
        count = 0
        async for event in bus.subscribe("zone-1"):
            received.append(event)
            count += 1
            if count >= 1:
                break

        # Should only receive the actual message, not the subscribe confirmation
        assert len(received) == 1
        assert received[0].path == "/test/file.txt"


# =============================================================================
# Error Recovery Tests
# =============================================================================


class TestErrorRecovery:
    """Tests for error handling and recovery scenarios."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        client = MagicMock()
        client.client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_publish_handles_redis_error(self, mock_redis_client):
        """Test that publish() handles Redis errors gracefully."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = True

        # Make publish raise an exception
        mock_redis_client.client.publish = AsyncMock(side_effect=Exception("Redis connection lost"))

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="zone-1",
        )

        # Should raise the error (caller should handle)
        with pytest.raises(Exception, match="Redis connection lost"):
            await bus.publish(event)

    @pytest.mark.asyncio
    async def test_wait_for_event_handles_malformed_json(self, mock_redis_client):
        """Test that wait_for_event() handles malformed JSON gracefully."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = True

        mock_pubsub = AsyncMock()
        mock_redis_client.client.pubsub.return_value = mock_pubsub

        # Return malformed JSON, then valid event
        valid_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="zone-1",
        )

        messages = [
            {"type": "message", "data": "not valid json{{{"},  # Malformed
            {"type": "message", "data": valid_event.to_json()},  # Valid
        ]
        call_count = [0]

        async def mock_get_message(*args, **kwargs):
            if call_count[0] < len(messages):
                msg = messages[call_count[0]]
                call_count[0] += 1
                return msg
            return None

        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()

        # Should skip malformed message and return valid one
        result = await bus.wait_for_event("zone-1", "/test/", timeout=1.0)

        assert result is not None
        assert result.path == "/test/file.txt"

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self, mock_redis_client):
        """Test that health_check() returns False on Redis error."""
        bus = RedisEventBus(mock_redis_client)
        bus._started = True

        # Make ping raise an exception
        mock_redis_client.client.ping = AsyncMock(side_effect=Exception("Connection refused"))

        result = await bus.health_check()
        assert result is False
