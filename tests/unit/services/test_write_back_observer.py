"""Tests for WriteBackService VFSObserver protocol (Issue #3194, #10A).

Tests the OBSERVE hook integration: on_mutation(), event_mask, hook_spec().
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.file_events import FILE_EVENT_BIT
from nexus.system_services.event_bus.types import FileEvent, FileEventType
from nexus.system_services.sync.write_back_service import (
    _WRITE_BACK_EVENT_MASK,
    WriteBackService,
)


def _make_service(**kwargs) -> WriteBackService:
    """Create a WriteBackService with mocked dependencies."""
    gw = kwargs.get("gateway") or MagicMock()
    bus = kwargs.get("event_bus") or AsyncMock()
    bus.subscribe = MagicMock(return_value=_empty_async_iter())
    bus.publish = AsyncMock(return_value=1)

    backlog = kwargs.get("backlog_store") or MagicMock()
    backlog.enqueue.return_value = True

    change_log = kwargs.get("change_log_store") or MagicMock()

    return WriteBackService(
        gateway=gw,
        event_bus=bus,
        backlog_store=backlog,
        change_log_store=change_log,
    )


async def _empty_async_iter():
    return
    yield  # pragma: no cover


# =============================================================================
# VFSObserver Protocol Tests
# =============================================================================


class TestVFSObserverProtocol:
    """Test that WriteBackService satisfies VFSObserver protocol."""

    def test_has_event_mask(self):
        """event_mask is a class attribute with correct bits."""
        assert hasattr(WriteBackService, "event_mask")
        mask = WriteBackService.event_mask
        assert isinstance(mask, int)
        assert mask > 0

    def test_event_mask_includes_only_mutation_events(self):
        """event_mask includes FILE_WRITE/DELETE/RENAME/DIR_CREATE/DIR_DELETE only."""
        mask = _WRITE_BACK_EVENT_MASK

        # Should include these
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_WRITE]
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_DELETE]
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_RENAME]
        assert mask & FILE_EVENT_BIT[FileEventType.DIR_CREATE]
        assert mask & FILE_EVENT_BIT[FileEventType.DIR_DELETE]

        # Should NOT include these (prevents feedback loops)
        assert not (mask & FILE_EVENT_BIT[FileEventType.SYNC_TO_BACKEND_COMPLETED])
        assert not (mask & FILE_EVENT_BIT[FileEventType.SYNC_TO_BACKEND_FAILED])
        assert not (mask & FILE_EVENT_BIT[FileEventType.CONFLICT_DETECTED])
        assert not (mask & FILE_EVENT_BIT[FileEventType.SYNC_TO_BACKEND_REQUESTED])

    def test_hook_spec_returns_correct_shape(self):
        """hook_spec() returns HookSpec with self as observer."""
        service = _make_service()
        spec = service.hook_spec()
        assert hasattr(spec, "observers")
        assert service in spec.observers

    @pytest.mark.asyncio
    async def test_drain_is_noop(self):
        """drain() completes without error."""
        service = _make_service()
        await service.drain()

    @pytest.mark.asyncio
    async def test_activate_is_noop(self):
        """activate() completes without error."""
        service = _make_service()
        await service.activate()

    def test_has_on_mutation(self):
        """on_mutation() method exists and is async."""
        import asyncio

        service = _make_service()
        assert hasattr(service, "on_mutation")
        assert asyncio.iscoroutinefunction(service.on_mutation)


# =============================================================================
# on_mutation() Event Handling Tests
# =============================================================================


class TestOnMutation:
    """Test on_mutation() delegates correctly to _on_file_event()."""

    @pytest.fixture
    def mock_gateway(self):
        gw = MagicMock()
        mock_backend = MagicMock()
        mock_backend.name = "test_gcs"
        mock_backend.capabilities = frozenset()
        gw.get_mount_for_path.return_value = {
            "mount_point": "/mnt/gcs",
            "backend": mock_backend,
            "backend_path": "project/file.txt",
            "readonly": False,
            "backend_name": "test_gcs",
            "conflict_strategy": None,
        }
        return gw

    @pytest.mark.asyncio
    async def test_on_mutation_enqueues_write_event(self, mock_gateway):
        """FILE_WRITE event via on_mutation() -> backlog enqueue."""
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/mnt/gcs/project/file.txt",
            zone_id="root",
            etag="abc123",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_called_once_with(
            path="/mnt/gcs/project/file.txt",
            backend_name="test_gcs",
            zone_id="root",
            operation_type="write",
            content_hash="abc123",
            new_path=None,
        )

    @pytest.mark.asyncio
    async def test_on_mutation_enqueues_delete_event(self, mock_gateway):
        """FILE_DELETE event via on_mutation() -> backlog enqueue with op=delete."""
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/mnt/gcs/project/file.txt",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_called_once()
        call_kwargs = service._backlog_store.enqueue.call_args[1]
        assert call_kwargs["operation_type"] == "delete"

    @pytest.mark.asyncio
    async def test_on_mutation_ignores_sync_events(self, mock_gateway):
        """SYNC_TO_BACKEND_COMPLETED events are not enqueued (no feedback loop)."""
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.SYNC_TO_BACKEND_COMPLETED,
            path="/mnt/gcs/project/file.txt",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_mutation_ignores_conflict_events(self, mock_gateway):
        """CONFLICT_DETECTED events are not enqueued (no feedback loop)."""
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.CONFLICT_DETECTED,
            path="/mnt/gcs/project/file.txt",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_mutation_skips_readonly_mounts(self, mock_gateway):
        """Events from readonly mounts are not enqueued."""
        mock_gateway.get_mount_for_path.return_value["readonly"] = True
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/mnt/gcs/project/file.txt",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_mutation_skips_unmounted_paths(self):
        """Events for unmounted paths are not enqueued."""
        gw = MagicMock()
        gw.get_mount_for_path.return_value = None
        service = _make_service(gateway=gw)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/unmounted/file.txt",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_mutation_handles_dir_create(self, mock_gateway):
        """DIR_CREATE event via on_mutation() -> backlog enqueue with op=mkdir."""
        service = _make_service(gateway=mock_gateway)

        event = FileEvent(
            type=FileEventType.DIR_CREATE,
            path="/mnt/gcs/project/subdir",
            zone_id="root",
        )
        await service.on_mutation(event)

        service._backlog_store.enqueue.assert_called_once()
        call_kwargs = service._backlog_store.enqueue.call_args[1]
        assert call_kwargs["operation_type"] == "mkdir"


# =============================================================================
# SyncBacklogStore on_enqueue Callback Tests
# =============================================================================


class TestOnEnqueueCallback:
    """Test the on_enqueue callback wiring in SyncBacklogStore."""

    def test_callback_fires_on_successful_enqueue(self):
        """on_enqueue callback is called after successful commit."""
        from nexus.storage.record_store import SQLAlchemyRecordStore
        from nexus.system_services.sync.sync_backlog_store import SyncBacklogStore

        store = SQLAlchemyRecordStore(db_url="sqlite:///:memory:", create_tables=True)
        callback = MagicMock()

        backlog = SyncBacklogStore(record_store=store, on_enqueue=callback)
        result = backlog.enqueue(
            path="/test/file.txt",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
        )

        assert result is True
        callback.assert_called_once()
        store.close()

    def test_callback_not_called_without_db(self):
        """on_enqueue callback is NOT called when record_store is None (no commit)."""
        from nexus.system_services.sync.sync_backlog_store import SyncBacklogStore

        callback = MagicMock()
        backlog = SyncBacklogStore(record_store=None, on_enqueue=callback)
        result = backlog.enqueue(
            path="/test/file.txt",
            backend_name="gcs",
            zone_id="root",
        )

        assert result is False
        callback.assert_not_called()

    def test_callback_exception_is_swallowed(self):
        """on_enqueue callback exception does not prevent enqueue from returning True."""
        from nexus.storage.record_store import SQLAlchemyRecordStore
        from nexus.system_services.sync.sync_backlog_store import SyncBacklogStore

        store = SQLAlchemyRecordStore(db_url="sqlite:///:memory:", create_tables=True)
        callback = MagicMock(side_effect=RuntimeError("pipe full"))

        backlog = SyncBacklogStore(record_store=store, on_enqueue=callback)
        result = backlog.enqueue(
            path="/test/file.txt",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
        )

        assert result is True  # Enqueue succeeded despite callback failure
        callback.assert_called_once()
        store.close()

    def test_no_callback_is_fine(self):
        """on_enqueue=None (default) works without error."""
        from nexus.storage.record_store import SQLAlchemyRecordStore
        from nexus.system_services.sync.sync_backlog_store import SyncBacklogStore

        store = SQLAlchemyRecordStore(db_url="sqlite:///:memory:", create_tables=True)
        backlog = SyncBacklogStore(record_store=store)
        result = backlog.enqueue(
            path="/test/file.txt",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
        )

        assert result is True
        store.close()
