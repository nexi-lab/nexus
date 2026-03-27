"""Unit tests for EventsService.

Tests file watching (delegated to kernel FileWatcher), advisory locking,
zone ID resolution, and infrastructure detection.

Architecture: EventsService is a thin RPC wrapper around kernel FileWatcher.
Local OBSERVE + remote watch logic lives in FileWatcher (tested separately
in tests/unit/core/test_file_watcher.py).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.file_events import FileEvent
from nexus.core.file_watcher import FileWatcher
from nexus.services.lifecycle.events_service import EventsService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def file_watcher():
    """Create a kernel FileWatcher instance."""
    return FileWatcher()


@pytest.fixture
def mock_lock_manager():
    """Create a mock distributed lock manager."""
    mgr = AsyncMock()
    mgr.acquire = AsyncMock(return_value="dist-lock-456")
    mgr.release = AsyncMock(return_value=True)
    extend_result = MagicMock()
    extend_result.success = True
    mgr.extend = AsyncMock(return_value=extend_result)
    return mgr


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


def _make_event(path: str = "/inbox/test.txt", event_type: str = "file_write") -> FileEvent:
    """Helper to create a FileEvent for testing."""
    return FileEvent(type=event_type, path=path, zone_id="root")


# =============================================================================
# Initialization
# =============================================================================


class TestEventsServiceInit:
    """Tests for EventsService construction."""

    def test_init_stores_file_watcher(self, file_watcher):
        """Service stores file watcher dependency."""
        svc = EventsService(
            file_watcher=file_watcher,
            zone_id="z1",
        )
        assert svc._file_watcher is file_watcher
        assert svc._lock_manager is not None  # LocalLockManager auto-created
        assert svc._zone_id == "z1"

    def test_upgrade_lock_manager(self, file_watcher, mock_lock_manager):
        """upgrade_lock_manager() replaces the default LocalLockManager."""
        svc = EventsService(file_watcher=file_watcher)
        original = svc._lock_manager
        assert original is not None
        svc.upgrade_lock_manager(mock_lock_manager)
        assert svc._lock_manager is mock_lock_manager
        assert svc._lock_manager is not original


# =============================================================================
# Infrastructure detection
# =============================================================================


class TestInfrastructureDetection:
    """Tests for layer detection methods."""

    def test_has_lock_manager_true_after_upgrade(self, file_watcher, mock_lock_manager):
        """Lock manager present after upgrade."""
        svc = EventsService(file_watcher=file_watcher)
        svc.upgrade_lock_manager(mock_lock_manager)
        assert svc._has_lock_manager() is True

    def test_has_lock_manager_always_true(self, file_watcher):
        """EventsService auto-creates local fallback — always has lock manager."""
        svc = EventsService(file_watcher=file_watcher)
        assert svc._has_lock_manager() is True


# =============================================================================
# Zone ID resolution
# =============================================================================


class TestZoneIdResolution:
    """Tests for _get_zone_id helper."""

    def test_uses_context_zone_id(self, file_watcher, context):
        """Zone ID comes from context when available."""
        svc = EventsService(file_watcher=file_watcher, zone_id="default_zone")
        assert svc._get_zone_id(context) == "test_zone"

    def test_falls_back_to_service_zone_id(self, file_watcher):
        """Falls back to service-level zone_id."""
        svc = EventsService(file_watcher=file_watcher, zone_id="service_zone")
        assert svc._get_zone_id(None) == "service_zone"

    def test_defaults_to_root(self, file_watcher):
        """Defaults to 'root' when no zone available."""
        svc = EventsService(file_watcher=file_watcher)
        assert svc._get_zone_id(None) == "root"


# =============================================================================
# wait_for_changes — delegates to FileWatcher
# =============================================================================


class TestWaitForChanges:
    """Tests for wait_for_changes delegation to kernel FileWatcher."""

    def test_timeout_returns_none(self, file_watcher):
        """Returns None when FileWatcher times out."""
        svc = EventsService(file_watcher=file_watcher)
        result = asyncio.run(svc.wait_for_changes("/data", timeout=0.05))
        assert result is None

    def test_returns_event_dict(self, file_watcher):
        """Returns event dict from FileWatcher."""
        event = _make_event("/data/file.txt")

        async def _test():
            svc = EventsService(file_watcher=file_watcher)
            wait_task = asyncio.create_task(svc.wait_for_changes("/data/file.txt", timeout=5.0))
            await asyncio.sleep(0.01)
            await file_watcher.on_mutation(event)
            return await wait_task

        result = asyncio.run(_test())
        assert result is not None
        assert result["path"] == "/data/file.txt"

    def test_passes_zone_id_from_context(self, file_watcher, context):
        """Zone ID from context is passed to FileWatcher.wait()."""
        svc = EventsService(file_watcher=file_watcher)
        # Just verify it doesn't crash with context — zone routing tested in FileWatcher tests
        result = asyncio.run(svc.wait_for_changes("/data", timeout=0.05, _context=context))
        assert result is None


# =============================================================================
# Advisory Locking — Distributed
# =============================================================================


class TestDistributedLocking:
    """Tests for locking via upgraded (distributed) lock manager."""

    def _make_svc(self, file_watcher, mock_lock_manager):
        """Create EventsService and upgrade to distributed lock manager."""
        svc = EventsService(file_watcher=file_watcher)
        svc.upgrade_lock_manager(mock_lock_manager)
        return svc

    def test_lock_acquires_distributed(self, file_watcher, mock_lock_manager):
        """Lock uses distributed lock manager when available."""
        svc = self._make_svc(file_watcher, mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=5.0, ttl=10.0))
        assert lock_id == "dist-lock-456"
        mock_lock_manager.acquire.assert_called_once()

    def test_lock_returns_none_on_timeout(self, file_watcher, mock_lock_manager):
        """Lock returns None when distributed lock times out."""
        mock_lock_manager.acquire = AsyncMock(return_value=None)
        svc = self._make_svc(file_watcher, mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=1.0))
        assert lock_id is None

    def test_unlock_releases_distributed(self, file_watcher, mock_lock_manager):
        """Unlock releases distributed lock."""
        svc = self._make_svc(file_watcher, mock_lock_manager)
        result = asyncio.run(svc.unlock("dist-lock-456", path="/data/file.txt"))
        assert result is True
        mock_lock_manager.release.assert_called_once()

    def test_unlock_requires_path_for_distributed(self, file_watcher, mock_lock_manager):
        """Distributed unlock requires path parameter."""
        svc = self._make_svc(file_watcher, mock_lock_manager)
        with pytest.raises(ValueError, match="path is required"):
            asyncio.run(svc.unlock("dist-lock-456", path=None))

    def test_extend_lock_distributed(self, file_watcher, mock_lock_manager):
        """Extend lock uses distributed lock manager."""
        svc = self._make_svc(file_watcher, mock_lock_manager)
        result = asyncio.run(svc.extend_lock("dist-lock-456", path="/data/file.txt", ttl=60.0))
        assert result is True
        mock_lock_manager.extend.assert_called_once()
