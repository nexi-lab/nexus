"""Tests for RevisionTrackingObserver (Issue #1382, #1748).

Verifies that the observer feeds RevisionNotifier on versioned VFS mutations
and skips events without version or zone_id.
"""

from __future__ import annotations

from nexus.core.file_events import ALL_FILE_EVENTS, FileEvent, FileEventType
from nexus.lib.revision_notifier import RevisionNotifier
from nexus.system_services.lifecycle.revision_tracking_observer import (
    RevisionTrackingObserver,
)


def _make_observer() -> tuple[RevisionTrackingObserver, RevisionNotifier]:
    notifier = RevisionNotifier()
    observer = RevisionTrackingObserver(revision_notifier=notifier)
    return observer, notifier


class TestRevisionTrackingObserver:
    async def test_write_event_updates_revision(self) -> None:
        """FILE_WRITE with version should update the notifier."""
        obs, notifier = _make_observer()
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="root",
            version=5,
        )
        await obs.on_mutation(event)
        assert notifier.get_latest_revision("root") == 5

    async def test_delete_event_with_version(self) -> None:
        """FILE_DELETE with version should also update revision."""
        obs, notifier = _make_observer()
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/test/gone.txt",
            zone_id="root",
            version=10,
        )
        await obs.on_mutation(event)
        assert notifier.get_latest_revision("root") == 10

    async def test_rename_event_with_version(self) -> None:
        """FILE_RENAME with version should update revision."""
        obs, notifier = _make_observer()
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/old.txt",
            zone_id="zone-a",
            version=7,
            new_path="/new.txt",
        )
        await obs.on_mutation(event)
        assert notifier.get_latest_revision("zone-a") == 7

    async def test_skips_event_without_version(self) -> None:
        """Events without version (e.g. DIR_CREATE) should be skipped."""
        obs, notifier = _make_observer()
        event = FileEvent(
            type=FileEventType.DIR_CREATE,
            path="/test/dir",
            zone_id="root",
        )
        await obs.on_mutation(event)
        assert notifier.get_latest_revision("root") == 0

    async def test_skips_event_without_zone_id(self) -> None:
        """Events without zone_id (Layer 1 local) should be skipped."""
        obs, notifier = _make_observer()
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            version=3,
        )
        await obs.on_mutation(event)
        # No zone_id → nothing tracked
        assert notifier.get_latest_revision("root") == 0

    async def test_zone_isolation(self) -> None:
        """Mutations in zone A should not affect zone B."""
        obs, notifier = _make_observer()
        await obs.on_mutation(
            FileEvent(type=FileEventType.FILE_WRITE, path="/a.txt", zone_id="zone-a", version=100)
        )
        assert notifier.get_latest_revision("zone-a") == 100
        assert notifier.get_latest_revision("zone-b") == 0

    async def test_monotonic_only_advances(self) -> None:
        """Revision should only go forward, never backward."""
        obs, notifier = _make_observer()
        await obs.on_mutation(
            FileEvent(type=FileEventType.FILE_WRITE, path="/a.txt", zone_id="root", version=10)
        )
        await obs.on_mutation(
            FileEvent(type=FileEventType.FILE_WRITE, path="/b.txt", zone_id="root", version=5)
        )
        assert notifier.get_latest_revision("root") == 10

    async def test_multiple_zones_tracked(self) -> None:
        """Multiple zones should be tracked independently."""
        obs, notifier = _make_observer()
        await obs.on_mutation(
            FileEvent(type=FileEventType.FILE_WRITE, path="/a.txt", zone_id="zone-a", version=3)
        )
        await obs.on_mutation(
            FileEvent(type=FileEventType.FILE_WRITE, path="/b.txt", zone_id="zone-b", version=7)
        )
        assert notifier.get_latest_revision("zone-a") == 3
        assert notifier.get_latest_revision("zone-b") == 7

    def test_event_mask_is_all(self) -> None:
        assert RevisionTrackingObserver.event_mask == ALL_FILE_EVENTS
