"""Unit tests for EventsService.

Tests file watching (delegated to kernel FileWatcher) and zone ID resolution.

Architecture: EventsService is a thin RPC wrapper around kernel FileWatcher.
Local OBSERVE + remote watch logic lives in FileWatcher (tested separately
in tests/unit/core/test_file_watcher.py).
"""

import asyncio

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
        assert svc._zone_id == "z1"


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
