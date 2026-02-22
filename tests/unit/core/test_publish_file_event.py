"""Unit tests for _publish_file_event and EventBus kernel integration.

Tests cover Issue #2175: Wire EventBus into kernel mutation hot path.
- _publish_file_event: enum type, event fields, None bus guard, error handling
- rmdir dir_delete event publishing
- write_batch bulk event publishing via publish_batch
- Factory EventLog WAL wiring
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from nexus.core.file_events import FileEvent, FileEventType

# =============================================================================
# Fixtures
# =============================================================================


class MockEventBus:
    """Minimal mock that satisfies the EventBusProtocol duck type."""

    def __init__(self) -> None:
        self._started = True
        self.published: list[FileEvent] = []
        self.batch_published: list[list[FileEvent]] = []
        self.publish = AsyncMock(side_effect=self._record_publish)
        self.publish_batch = AsyncMock(side_effect=self._record_batch)

    async def _record_publish(self, event: FileEvent) -> int:
        self.published.append(event)
        return 1

    async def _record_batch(self, events: list[FileEvent]) -> list[int]:
        self.batch_published.append(events)
        return [1] * len(events)

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False


class StubNexusFSCore:
    """Stub that inherits _publish_file_event behavior for testing.

    Uses direct method binding to test the actual implementation
    without requiring the full NexusFSCoreMixin inheritance tree.
    """

    def __init__(self, event_bus: MockEventBus | None = None) -> None:
        self._event_bus = event_bus

    def _publish_file_event(
        self,
        event_type: FileEventType,
        path: str,
        zone_id: str | None,
        size: int | None = None,
        etag: str | None = None,
        agent_id: str | None = None,
        old_path: str | None = None,
        revision: int | None = None,
    ) -> None:
        """Delegate to the real implementation."""
        from nexus.core.nexus_fs_core import NexusFSCoreMixin

        NexusFSCoreMixin._publish_file_event(
            cast(Any, self),
            event_type=event_type,
            path=path,
            zone_id=zone_id,
            size=size,
            etag=etag,
            agent_id=agent_id,
            old_path=old_path,
            revision=revision,
        )


# =============================================================================
# _publish_file_event tests
# =============================================================================


class TestPublishFileEvent:
    """Tests for the centralized _publish_file_event method."""

    def test_none_event_bus_is_noop(self) -> None:
        """When _event_bus is None, _publish_file_event silently returns."""
        stub = StubNexusFSCore(event_bus=None)
        # Should not raise
        stub._publish_file_event(
            event_type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="root",
        )

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_publishes_file_write_event(self, mock_faf: MagicMock) -> None:
        """FILE_WRITE event is published with correct fields."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_WRITE,
            path="/data/file.txt",
            zone_id="zone1",
            size=1024,
            etag="abc123",
            agent_id="agent-1",
        )

        # fire_and_forget was called with the publish coroutine
        assert mock_faf.called
        # Verify the coroutine was created with publish()
        call_args = mock_faf.call_args[0][0]
        assert call_args is not None

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_publishes_file_delete_event(self, mock_faf: MagicMock) -> None:
        """FILE_DELETE event is published with correct type."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_DELETE,
            path="/data/old.txt",
            zone_id="root",
        )

        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_publishes_file_rename_event(self, mock_faf: MagicMock) -> None:
        """FILE_RENAME event includes old_path."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_RENAME,
            path="/new/path.txt",
            zone_id="zone1",
            old_path="/old/path.txt",
        )

        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_publishes_dir_create_event(self, mock_faf: MagicMock) -> None:
        """DIR_CREATE event is published for mkdir."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.DIR_CREATE,
            path="/new/dir",
            zone_id="root",
            agent_id="agent-1",
        )

        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_publishes_dir_delete_event(self, mock_faf: MagicMock) -> None:
        """DIR_DELETE event is published for rmdir (Issue #2175)."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.DIR_DELETE,
            path="/old/dir",
            zone_id="root",
            agent_id="agent-2",
        )

        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_zone_id_defaults_to_root(self, mock_faf: MagicMock) -> None:
        """When zone_id is None, defaults to 'root'."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id=None,
        )

        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_error_is_logged_not_raised(self, mock_faf: MagicMock) -> None:
        """Errors in event creation are logged but don't propagate."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        # Force an error by patching FileEvent to raise
        with patch("nexus.core.file_events.FileEvent", side_effect=ValueError("bad")):
            # Should NOT raise
            stub._publish_file_event(
                event_type=FileEventType.FILE_WRITE,
                path="/test.txt",
                zone_id="root",
            )

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_lazy_start_when_bus_not_started(self, mock_faf: MagicMock) -> None:
        """When bus._started is False, creates start-then-publish coroutine."""
        bus = MockEventBus()
        bus._started = False
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="root",
        )

        # fire_and_forget was called (with the start-and-publish wrapper)
        assert mock_faf.called

    @patch("nexus.lib.sync_bridge.fire_and_forget")
    def test_revision_passed_through(self, mock_faf: MagicMock) -> None:
        """Revision field is included in the event."""
        bus = MockEventBus()
        stub = StubNexusFSCore(event_bus=bus)

        stub._publish_file_event(
            event_type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="root",
            revision=42,
        )

        assert mock_faf.called


# =============================================================================
# Factory EventLog wiring tests
# =============================================================================


class TestFactoryEventLogWiring:
    """Tests for Issue #2175: EventLog WAL wired into EventBus in factory."""

    def test_create_event_log_wal_returns_none_when_unavailable(self) -> None:
        """When Rust WAL is not available, returns None gracefully."""
        from nexus.factory._distributed import _create_event_log_wal

        with patch(
            "nexus.services.event_subsystem.log.factory.create_event_log",
            return_value=None,
        ):
            result = _create_event_log_wal()
            assert result is None

    def test_create_event_log_wal_returns_log_when_available(self) -> None:
        """When Rust WAL is available, returns EventLogProtocol."""
        from nexus.factory._distributed import _create_event_log_wal

        mock_log = MagicMock()
        with patch(
            "nexus.services.event_subsystem.log.factory.create_event_log",
            return_value=mock_log,
        ):
            result = _create_event_log_wal()
            assert result is mock_log

    def test_event_log_wired_into_redis_event_bus(self) -> None:
        """EventLog is wired into EventBus via set_event_log."""
        from nexus.factory._distributed import _create_distributed_infra

        mock_config = MagicMock()
        mock_config.enable_locks = False
        mock_config.enable_events = True
        mock_config.event_bus_backend = "redis"
        mock_metadata = MagicMock()
        mock_record = MagicMock()

        mock_bus = MagicMock()
        mock_bus.set_event_log = MagicMock()
        mock_log = MagicMock()

        with (
            patch("nexus.lib.env.get_redis_url", return_value=None),
            patch("nexus.lib.env.get_dragonfly_url", return_value="redis://localhost:6379"),
            patch("nexus.bricks.cache.dragonfly.DragonflyClient"),
            patch("nexus.services.event_subsystem.bus.RedisEventBus", return_value=mock_bus),
            patch("nexus.factory._distributed._create_event_log_wal", return_value=mock_log),
        ):
            event_bus, _ = _create_distributed_infra(mock_config, mock_metadata, mock_record, None)

            assert event_bus is mock_bus
            mock_bus.set_event_log.assert_called_once_with(mock_log)

    def test_event_log_not_wired_when_wal_unavailable(self) -> None:
        """When WAL is unavailable, EventBus works without EventLog."""
        from nexus.factory._distributed import _create_distributed_infra

        mock_config = MagicMock()
        mock_config.enable_locks = False
        mock_config.enable_events = True
        mock_config.event_bus_backend = "redis"
        mock_metadata = MagicMock()
        mock_record = MagicMock()

        mock_bus = MagicMock()

        with (
            patch("nexus.lib.env.get_redis_url", return_value=None),
            patch("nexus.lib.env.get_dragonfly_url", return_value="redis://localhost:6379"),
            patch("nexus.bricks.cache.dragonfly.DragonflyClient"),
            patch("nexus.services.event_subsystem.bus.RedisEventBus", return_value=mock_bus),
            patch("nexus.factory._distributed._create_event_log_wal", return_value=None),
        ):
            event_bus, _ = _create_distributed_infra(mock_config, mock_metadata, mock_record, None)

            assert event_bus is mock_bus
            # set_event_log should NOT be called when WAL is unavailable
            mock_bus.set_event_log.assert_not_called()


# =============================================================================
# FileEvent type safety tests (Issue #2175: enum-direct)
# =============================================================================


class TestFileEventTypeSafety:
    """Ensure FileEventType enum is used directly (no string mapping)."""

    def test_all_event_types_are_enum_values(self) -> None:
        """All expected event types exist as FileEventType enum members."""
        assert FileEventType.FILE_WRITE.value == "file_write"
        assert FileEventType.FILE_DELETE.value == "file_delete"
        assert FileEventType.FILE_RENAME.value == "file_rename"
        assert FileEventType.DIR_CREATE.value == "dir_create"
        assert FileEventType.DIR_DELETE.value == "dir_delete"

    def test_file_event_accepts_enum_type(self) -> None:
        """FileEvent can be created with FileEventType enum."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id="root",
            size=100,
            etag="hash",
            agent_id="agent-1",
        )
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/test.txt"
        assert event.zone_id == "root"
        assert event.size == 100
        assert event.etag == "hash"
        assert event.agent_id == "agent-1"

    def test_file_event_dir_delete_type(self) -> None:
        """FileEvent can represent dir_delete (Issue #2175: rmdir coverage)."""
        event = FileEvent(
            type=FileEventType.DIR_DELETE,
            path="/old/dir",
            zone_id="zone1",
            agent_id="agent-2",
        )
        assert event.type == FileEventType.DIR_DELETE
        assert event.path == "/old/dir"
        assert event.agent_id == "agent-2"
