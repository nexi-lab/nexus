"""Unit tests for EventsService.

Tests dual-track infrastructure detection, advisory locking,
and cache invalidation.

All async service methods are tested via asyncio.run().
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.protocols.connector import PassthroughProtocol
from nexus.system_services.lifecycle.events_service import EventsService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_backend_passthrough():
    """Create a mock passthrough backend (same-box mode).

    Uses create_autospec so isinstance(mock, PassthroughProtocol)
    works on Python 3.12+ where @runtime_checkable checks are stricter.
    """
    backend = create_autospec(PassthroughProtocol, instance=True)
    backend.is_passthrough = True
    backend.base_path = "/tmp/test_data"
    backend.lock = MagicMock(return_value="lock-123")
    backend.unlock = MagicMock(return_value=True)
    return backend


@pytest.fixture
def mock_backend_remote():
    """Create a mock non-passthrough backend (distributed mode)."""
    backend = MagicMock()
    backend.is_passthrough = False
    return backend


@pytest.fixture
def mock_event_bus():
    """Create a mock distributed event bus."""
    bus = AsyncMock()
    bus._started = True
    bus.start = AsyncMock()
    bus.wait_for_event = AsyncMock(return_value=None)
    bus.subscribe = AsyncMock()
    return bus


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
def mock_file_watcher():
    """Create a mock file watcher."""
    watcher = MagicMock()
    watcher._started = True
    watcher.wait_for_change = AsyncMock(return_value=None)
    watcher.add_watch = MagicMock()
    watcher.stop = MagicMock()
    return watcher


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


# =============================================================================
# Initialization
# =============================================================================


class TestEventsServiceInit:
    """Tests for EventsService construction."""

    def test_init_stores_all_dependencies(
        self,
        mock_backend_passthrough,
        mock_event_bus,
        mock_lock_manager,
    ):
        """Service stores all injected dependencies."""
        svc = EventsService(
            backend=mock_backend_passthrough,
            event_bus=mock_event_bus,
            lock_manager=mock_lock_manager,
            zone_id="z1",
        )
        assert svc._backend is mock_backend_passthrough
        assert svc._event_bus is mock_event_bus
        assert svc._lock_manager is mock_lock_manager
        assert svc._file_watcher is None  # lazy-initialized, not injected
        assert svc._zone_id == "z1"

    def test_init_minimal(self, mock_backend_remote):
        """Service can be created with just a backend."""
        svc = EventsService(backend=mock_backend_remote)
        assert svc._event_bus is None
        assert svc._lock_manager is None
        assert svc._file_watcher is None
        assert svc._zone_id is None


# =============================================================================
# Infrastructure detection
# =============================================================================


class TestInfrastructureDetection:
    """Tests for layer detection methods."""

    def test_is_same_box_true(self, mock_backend_passthrough):
        """Passthrough backend means same-box."""
        svc = EventsService(backend=mock_backend_passthrough)
        assert svc._is_same_box() is True

    def test_is_same_box_false(self, mock_backend_remote):
        """Non-passthrough backend is not same-box."""
        svc = EventsService(backend=mock_backend_remote)
        assert svc._is_same_box() is False

    def test_has_distributed_events_true(self, mock_backend_remote, mock_event_bus):
        """Event bus present means distributed events available."""
        svc = EventsService(backend=mock_backend_remote, event_bus=mock_event_bus)
        assert svc._has_distributed_events() is True

    def test_has_distributed_events_false(self, mock_backend_remote):
        """No event bus means no distributed events."""
        svc = EventsService(backend=mock_backend_remote)
        assert svc._has_distributed_events() is False

    def test_has_distributed_locks_true(self, mock_backend_remote, mock_lock_manager):
        """Lock manager present means distributed locks available."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        assert svc._has_distributed_locks() is True

    def test_has_distributed_locks_false(self, mock_backend_remote):
        """No lock manager means no distributed locks."""
        svc = EventsService(backend=mock_backend_remote)
        assert svc._has_distributed_locks() is False


# =============================================================================
# Zone ID resolution
# =============================================================================


class TestZoneIdResolution:
    """Tests for _get_zone_id helper."""

    def test_uses_context_zone_id(self, mock_backend_remote, context):
        """Zone ID comes from context when available."""
        svc = EventsService(backend=mock_backend_remote, zone_id="default_zone")
        assert svc._get_zone_id(context) == "test_zone"

    def test_falls_back_to_service_zone_id(self, mock_backend_remote):
        """Falls back to service-level zone_id."""
        svc = EventsService(backend=mock_backend_remote, zone_id="service_zone")
        assert svc._get_zone_id(None) == "service_zone"

    def test_defaults_to_default(self, mock_backend_remote):
        """Defaults to 'root' when no zone available."""
        svc = EventsService(backend=mock_backend_remote)
        assert svc._get_zone_id(None) == "root"


# =============================================================================
# File watcher lazy init
# =============================================================================


class TestFileWatcherLazyInit:
    """Tests for _get_file_watcher lazy initialization."""

    def test_lazy_creates_watcher(self, mock_backend_passthrough):
        """Lazy-creates FileWatcher for passthrough backend."""
        svc = EventsService(backend=mock_backend_passthrough)
        watcher = svc._get_file_watcher()
        assert watcher is not None
        assert svc._file_watcher is watcher

    def test_raises_for_non_passthrough(self, mock_backend_remote):
        """Raises NotImplementedError for non-passthrough backend."""
        svc = EventsService(backend=mock_backend_remote)
        with pytest.raises(NotImplementedError, match="PassthroughBackend"):
            svc._get_file_watcher()


# =============================================================================
# Advisory Locking — Distributed
# =============================================================================


class TestDistributedLocking:
    """Tests for locking via distributed lock manager."""

    def test_lock_acquires_distributed(self, mock_backend_remote, mock_lock_manager):
        """Lock uses distributed lock manager when available."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=5.0, ttl=10.0))
        assert lock_id == "dist-lock-456"
        mock_lock_manager.acquire.assert_called_once()

    def test_lock_returns_none_on_timeout(self, mock_backend_remote, mock_lock_manager):
        """Lock returns None when distributed lock times out."""
        mock_lock_manager.acquire = AsyncMock(return_value=None)
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=1.0))
        assert lock_id is None

    def test_unlock_releases_distributed(self, mock_backend_remote, mock_lock_manager):
        """Unlock releases distributed lock."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        result = asyncio.run(svc.unlock("dist-lock-456", path="/data/file.txt"))
        assert result is True
        mock_lock_manager.release.assert_called_once()

    def test_unlock_requires_path_for_distributed(self, mock_backend_remote, mock_lock_manager):
        """Distributed unlock requires path parameter."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        with pytest.raises(ValueError, match="path is required"):
            asyncio.run(svc.unlock("dist-lock-456", path=None))

    def test_extend_lock_distributed(self, mock_backend_remote, mock_lock_manager):
        """Extend lock uses distributed lock manager."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)
        result = asyncio.run(svc.extend_lock("dist-lock-456", path="/data/file.txt", ttl=60.0))
        assert result is True
        mock_lock_manager.extend.assert_called_once()


# =============================================================================
# Advisory Locking — Same-Box
# =============================================================================


class TestSameBoxLocking:
    """Tests for locking via PassthroughBackend."""

    def test_lock_uses_backend(self, mock_backend_passthrough):
        """Lock delegates to backend.lock() for same-box."""
        svc = EventsService(backend=mock_backend_passthrough)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=5.0))
        assert lock_id == "lock-123"
        mock_backend_passthrough.lock.assert_called_once()

    def test_unlock_uses_backend(self, mock_backend_passthrough):
        """Unlock delegates to backend.unlock() for same-box."""
        svc = EventsService(backend=mock_backend_passthrough)
        result = asyncio.run(svc.unlock("lock-123"))
        assert result is True
        mock_backend_passthrough.unlock.assert_called_once_with("lock-123")

    def test_extend_lock_noop_same_box(self, mock_backend_passthrough):
        """Extend lock is a no-op for same-box (no TTL)."""
        svc = EventsService(backend=mock_backend_passthrough)
        result = asyncio.run(svc.extend_lock("lock-123", path="/data/file.txt"))
        assert result is True


# =============================================================================
# Locking — No Infrastructure
# =============================================================================


class TestLockingNoInfrastructure:
    """Tests for locking when no lock infrastructure is available."""

    def test_lock_raises_not_implemented(self, mock_backend_remote):
        """Lock raises NotImplementedError without any lock manager."""
        svc = EventsService(backend=mock_backend_remote)
        with pytest.raises(NotImplementedError, match="No lock manager"):
            asyncio.run(svc.lock("/data/file.txt"))

    def test_unlock_raises_not_implemented(self, mock_backend_remote):
        """Unlock raises NotImplementedError without any lock manager."""
        svc = EventsService(backend=mock_backend_remote)
        with pytest.raises(NotImplementedError, match="No lock manager"):
            asyncio.run(svc.unlock("lock-123", path="/data/file.txt"))

    def test_extend_raises_not_implemented(self, mock_backend_remote):
        """Extend raises NotImplementedError without any lock manager."""
        svc = EventsService(backend=mock_backend_remote)
        with pytest.raises(NotImplementedError, match="No lock manager"):
            asyncio.run(svc.extend_lock("lock-123", path="/data/file.txt"))


# =============================================================================
# wait_for_changes — No Infrastructure
# =============================================================================


class TestWaitForChangesNoInfra:
    """Tests for wait_for_changes when no event source is available."""

    def test_raises_not_implemented(self, mock_backend_remote):
        """Raises NotImplementedError without event source."""
        svc = EventsService(backend=mock_backend_remote)
        with pytest.raises(NotImplementedError, match="No event source"):
            asyncio.run(svc.wait_for_changes("/data"))


# =============================================================================
# wait_for_changes — Distributed
# =============================================================================


class TestWaitForChangesDistributed:
    """Tests for wait_for_changes with distributed event bus."""

    def test_returns_none_on_timeout(self, mock_backend_remote, mock_event_bus):
        """Returns None when event bus times out."""
        mock_event_bus.wait_for_event = AsyncMock(return_value=None)
        svc = EventsService(backend=mock_backend_remote, event_bus=mock_event_bus)
        result = asyncio.run(svc.wait_for_changes("/data", timeout=1.0))
        assert result is None

    def test_returns_event_dict(self, mock_backend_remote, mock_event_bus):
        """Returns event dict from bus."""
        mock_event = MagicMock()
        mock_event.to_dict.return_value = {"type": "write", "path": "/data/file.txt"}
        mock_event_bus.wait_for_event = AsyncMock(return_value=mock_event)
        svc = EventsService(backend=mock_backend_remote, event_bus=mock_event_bus)
        result = asyncio.run(svc.wait_for_changes("/data"))
        assert result == {"type": "write", "path": "/data/file.txt"}


# =============================================================================
# Cache invalidation
# =============================================================================


# =============================================================================
# locked() context manager
# =============================================================================


class TestLockedContextManager:
    """Tests for the locked() async context manager."""

    def test_locked_raises_on_timeout(self, mock_backend_remote, mock_lock_manager):
        """locked() raises LockTimeout when lock acquisition fails."""
        from nexus.contracts.exceptions import LockTimeout

        mock_lock_manager.acquire = AsyncMock(return_value=None)
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)

        async def _test():
            async with svc.locked("/data/file.txt", timeout=1.0):
                pass  # pragma: no cover

        with pytest.raises(LockTimeout):
            asyncio.run(_test())

    def test_locked_releases_on_exit(self, mock_backend_remote, mock_lock_manager):
        """locked() releases lock on context exit."""
        svc = EventsService(backend=mock_backend_remote, lock_manager=mock_lock_manager)

        async def _test():
            async with svc.locked("/data/file.txt") as lock_id:
                assert lock_id == "dist-lock-456"

        asyncio.run(_test())
        mock_lock_manager.release.assert_called_once()
