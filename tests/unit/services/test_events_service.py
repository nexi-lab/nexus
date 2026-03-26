"""Unit tests for EventsService.

Tests OBSERVE-based internal watch, EventBus distributed watch,
race mechanism, advisory locking, and infrastructure detection.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.file_events import FileEvent
from nexus.services.lifecycle.events_service import EventsService

# =============================================================================
# Fixtures
# =============================================================================


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

    def test_init_stores_all_dependencies(self, mock_event_bus, mock_lock_manager):
        """Service stores all injected dependencies."""
        svc = EventsService(
            event_bus=mock_event_bus,
            lock_manager=mock_lock_manager,
            zone_id="z1",
        )
        assert svc._event_bus is mock_event_bus
        assert svc._lock_manager is mock_lock_manager
        assert svc._zone_id == "z1"
        assert svc._observe_registered is True  # hooks registered at enlist() time

    def test_init_minimal(self):
        """Service can be created with no dependencies — local lock fallback auto-created."""
        svc = EventsService()
        assert svc._event_bus is None
        assert svc._lock_manager is not None  # local fallback auto-created
        assert svc._zone_id is None
        assert svc._observe_registered is True  # hooks registered at enlist() time


# =============================================================================
# HookSpec (Issue #1611)
# =============================================================================


class TestHookSpec:
    """EventsService exposes hook_spec."""

    def test_isinstance_hot_swappable(self):
        """hasattr check passes — coordinator auto-detects hook_spec."""
        svc = EventsService()
        assert hasattr(svc, "hook_spec")

    def test_hook_spec_returns_observer(self):
        """hook_spec() declares self as the sole observer."""
        svc = EventsService()
        spec = svc.hook_spec()
        assert spec.observers == (svc,)
        assert spec.total_hooks == 1


# =============================================================================
# Infrastructure detection
# =============================================================================


class TestInfrastructureDetection:
    """Tests for layer detection methods."""

    def test_has_internal_observe_true_by_default(self):
        """Observer registered at enlist() time — defaults to True."""
        svc = EventsService()
        assert svc._has_internal_observe() is True

    def test_has_internal_observe_true_after_registration(self):
        """True after factory sets _observe_registered."""
        svc = EventsService()
        svc._observe_registered = True
        assert svc._has_internal_observe() is True

    def test_has_distributed_events_true(self, mock_event_bus):
        """Event bus present means distributed events available."""
        svc = EventsService(event_bus=mock_event_bus)
        assert svc._has_distributed_events() is True

    def test_has_distributed_events_false(self):
        """No event bus means no distributed events."""
        svc = EventsService()
        assert svc._has_distributed_events() is False

    def test_has_lock_manager_true(self, mock_lock_manager):
        """Lock manager present means distributed locks available."""
        svc = EventsService(lock_manager=mock_lock_manager)
        assert svc._has_lock_manager() is True

    def test_has_lock_manager_always_true(self):
        """EventsService auto-creates local fallback — always has lock manager."""
        svc = EventsService()
        assert svc._has_lock_manager() is True


# =============================================================================
# Zone ID resolution
# =============================================================================


class TestZoneIdResolution:
    """Tests for _get_zone_id helper."""

    def test_uses_context_zone_id(self, context):
        """Zone ID comes from context when available."""
        svc = EventsService(zone_id="default_zone")
        assert svc._get_zone_id(context) == "test_zone"

    def test_falls_back_to_service_zone_id(self):
        """Falls back to service-level zone_id."""
        svc = EventsService(zone_id="service_zone")
        assert svc._get_zone_id(None) == "service_zone"

    def test_defaults_to_root(self):
        """Defaults to 'root' when no zone available."""
        svc = EventsService()
        assert svc._get_zone_id(None) == "root"


# =============================================================================
# on_mutation — VFSObserver callback
# =============================================================================


class TestOnMutation:
    """Tests for on_mutation() — OBSERVE callback."""

    def test_on_mutation_resolves_matching_waiter(self):
        """on_mutation fires → pending wait_for_changes receives event."""
        svc = EventsService()
        svc._observe_registered = True

        event = _make_event("/inbox/test.txt")

        async def _test():
            # Start waiting in background
            wait_task = asyncio.create_task(svc._wait_internal("/inbox/test.txt", timeout=5.0))
            # Give the waiter time to register
            await asyncio.sleep(0.01)
            # Fire the event (simulating dispatch.notify)
            await svc.on_mutation(event)
            result = await wait_task
            assert result is event

        asyncio.run(_test())

    def test_on_mutation_skips_non_matching_waiter(self):
        """on_mutation with non-matching path does not resolve waiter."""
        svc = EventsService()
        svc._observe_registered = True

        event = _make_event("/other/file.txt")

        async def _test():
            wait_task = asyncio.create_task(svc._wait_internal("/inbox/*.txt", timeout=0.1))
            await asyncio.sleep(0.01)
            await svc.on_mutation(event)
            result = await wait_task
            assert result is None  # timeout, not matched

        asyncio.run(_test())

    def test_on_mutation_pattern_matching(self):
        """on_mutation matches glob patterns."""
        svc = EventsService()
        svc._observe_registered = True

        event = _make_event("/docs/report.md")

        async def _test():
            wait_task = asyncio.create_task(svc._wait_internal("/docs/*.md", timeout=5.0))
            await asyncio.sleep(0.01)
            await svc.on_mutation(event)
            result = await wait_task
            assert result is event

        asyncio.run(_test())

    def test_on_mutation_directory_pattern(self):
        """on_mutation matches directory patterns."""
        svc = EventsService()
        svc._observe_registered = True

        event = _make_event("/inbox/subdir/file.txt")

        async def _test():
            wait_task = asyncio.create_task(svc._wait_internal("/inbox/", timeout=5.0))
            await asyncio.sleep(0.01)
            await svc.on_mutation(event)
            result = await wait_task
            assert result is event

        asyncio.run(_test())


# =============================================================================
# wait_for_changes — Internal path only
# =============================================================================


class TestWaitForChangesInternal:
    """Tests for wait_for_changes with only internal OBSERVE."""

    def test_timeout_returns_none(self):
        """Returns None when internal path times out."""
        svc = EventsService()
        svc._observe_registered = True
        result = asyncio.run(svc.wait_for_changes("/data", timeout=0.05))
        assert result is None

    def test_returns_event_dict(self):
        """Returns event dict from internal observer."""
        svc = EventsService()
        svc._observe_registered = True

        event = _make_event("/data/file.txt")

        async def _test():
            wait_task = asyncio.create_task(svc.wait_for_changes("/data/file.txt", timeout=5.0))
            await asyncio.sleep(0.01)
            await svc.on_mutation(event)
            return await wait_task

        result = asyncio.run(_test())
        assert result is not None
        assert result["path"] == "/data/file.txt"


# =============================================================================
# wait_for_changes — EventBus only
# =============================================================================


class TestWaitForChangesEventBus:
    """Tests for wait_for_changes with only distributed event bus."""

    def test_returns_none_on_timeout(self, mock_event_bus):
        """Returns None when event bus times out."""
        mock_event_bus.wait_for_event = AsyncMock(return_value=None)
        svc = EventsService(event_bus=mock_event_bus)
        result = asyncio.run(svc.wait_for_changes("/data", timeout=1.0))
        assert result is None

    def test_returns_event_dict(self, mock_event_bus):
        """Returns event dict from bus."""
        mock_event = MagicMock()
        mock_event.to_dict.return_value = {"type": "file_write", "path": "/data/file.txt"}
        mock_event_bus.wait_for_event = AsyncMock(return_value=mock_event)
        svc = EventsService(event_bus=mock_event_bus)
        result = asyncio.run(svc.wait_for_changes("/data"))
        assert result == {"type": "file_write", "path": "/data/file.txt"}


# =============================================================================
# wait_for_changes — Race (internal + EventBus)
# =============================================================================


class TestWaitForChangesRace:
    """Tests for wait_for_changes when both paths are available."""

    def test_internal_wins_race(self, mock_event_bus):
        """Internal observer fires first → EventBus task cancelled."""

        # EventBus hangs forever (simulating slow remote path)
        async def _hang(**kwargs):
            await asyncio.sleep(999)

        mock_event_bus.wait_for_event = AsyncMock(side_effect=_hang)
        svc = EventsService(event_bus=mock_event_bus)
        svc._observe_registered = True

        event = _make_event("/data/file.txt")

        async def _test():
            wait_task = asyncio.create_task(svc.wait_for_changes("/data/file.txt", timeout=5.0))
            await asyncio.sleep(0.01)
            await svc.on_mutation(event)
            return await wait_task

        result = asyncio.run(_test())
        assert result is not None
        assert result["path"] == "/data/file.txt"

    def test_eventbus_wins_race(self, mock_event_bus):
        """EventBus fires first → internal task cancelled."""
        mock_event = MagicMock()
        mock_event.to_dict.return_value = {"type": "file_write", "path": "/data/file.txt"}
        mock_event_bus.wait_for_event = AsyncMock(return_value=mock_event)
        svc = EventsService(event_bus=mock_event_bus)
        svc._observe_registered = True

        result = asyncio.run(svc.wait_for_changes("/data/file.txt", timeout=5.0))
        assert result is not None
        assert result["path"] == "/data/file.txt"


# =============================================================================
# wait_for_changes — No Infrastructure
# =============================================================================


class TestWaitForChangesNoInfra:
    """Tests for wait_for_changes when no event source is available."""

    def test_raises_not_implemented(self):
        """Raises NotImplementedError without any event source."""
        svc = EventsService()
        svc._observe_registered = False  # simulate pre-enlist state
        with pytest.raises(NotImplementedError, match="No event source"):
            asyncio.run(svc.wait_for_changes("/data"))


# =============================================================================
# Advisory Locking — Distributed
# =============================================================================


class TestDistributedLocking:
    """Tests for locking via distributed lock manager."""

    def test_lock_acquires_distributed(self, mock_lock_manager):
        """Lock uses distributed lock manager when available."""
        svc = EventsService(lock_manager=mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=5.0, ttl=10.0))
        assert lock_id == "dist-lock-456"
        mock_lock_manager.acquire.assert_called_once()

    def test_lock_returns_none_on_timeout(self, mock_lock_manager):
        """Lock returns None when distributed lock times out."""
        mock_lock_manager.acquire = AsyncMock(return_value=None)
        svc = EventsService(lock_manager=mock_lock_manager)
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=1.0))
        assert lock_id is None

    def test_unlock_releases_distributed(self, mock_lock_manager):
        """Unlock releases distributed lock."""
        svc = EventsService(lock_manager=mock_lock_manager)
        result = asyncio.run(svc.unlock("dist-lock-456", path="/data/file.txt"))
        assert result is True
        mock_lock_manager.release.assert_called_once()

    def test_unlock_requires_path_for_distributed(self, mock_lock_manager):
        """Distributed unlock requires path parameter."""
        svc = EventsService(lock_manager=mock_lock_manager)
        with pytest.raises(ValueError, match="path is required"):
            asyncio.run(svc.unlock("dist-lock-456", path=None))

    def test_extend_lock_distributed(self, mock_lock_manager):
        """Extend lock uses distributed lock manager."""
        svc = EventsService(lock_manager=mock_lock_manager)
        result = asyncio.run(svc.extend_lock("dist-lock-456", path="/data/file.txt", ttl=60.0))
        assert result is True
        mock_lock_manager.extend.assert_called_once()


# =============================================================================
# Locking — No Infrastructure
# =============================================================================


class TestLockingLocalFallback:
    """Tests for locking with local SemaphoreAdvisoryLockManager fallback."""

    def test_lock_uses_local_fallback(self):
        """EventsService auto-creates local lock manager when none provided."""
        svc = EventsService()
        assert svc._has_lock_manager() is True
        lock_id = asyncio.run(svc.lock("/data/file.txt", timeout=1.0))
        assert lock_id is not None

    def test_unlock_with_local_fallback(self):
        """Unlock works with local fallback lock manager."""
        svc = EventsService()

        async def _test():
            lock_id = await svc.lock("/data/file.txt", timeout=1.0)
            assert lock_id is not None
            result = await svc.unlock(lock_id, path="/data/file.txt")
            assert result is True

        asyncio.run(_test())

    def test_extend_with_local_fallback(self):
        """Extend works with local fallback lock manager."""
        svc = EventsService()

        async def _test():
            lock_id = await svc.lock("/data/file.txt", timeout=1.0)
            assert lock_id is not None
            result = await svc.extend_lock(lock_id, path="/data/file.txt", ttl=60.0)
            assert result is True

        asyncio.run(_test())


# =============================================================================
# locked() context manager
# =============================================================================


class TestLockedContextManager:
    """Tests for the locked() async context manager."""

    def test_locked_raises_on_timeout(self, mock_lock_manager):
        """locked() raises LockTimeout when lock acquisition fails."""
        from nexus.contracts.exceptions import LockTimeout

        mock_lock_manager.acquire = AsyncMock(return_value=None)
        svc = EventsService(lock_manager=mock_lock_manager)

        async def _test():
            async with svc.locked("/data/file.txt", timeout=1.0):
                pass  # pragma: no cover

        with pytest.raises(LockTimeout):
            asyncio.run(_test())

    def test_locked_releases_on_exit(self, mock_lock_manager):
        """locked() releases lock on context exit."""
        svc = EventsService(lock_manager=mock_lock_manager)

        async def _test():
            async with svc.locked("/data/file.txt") as lock_id:
                assert lock_id == "dist-lock-456"

        asyncio.run(_test())
        mock_lock_manager.release.assert_called_once()
