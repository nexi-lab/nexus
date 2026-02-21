"""Integration tests for DedupWorkQueue with EventsService (Issue #2062).

Validates that the DedupWorkQueue correctly coalesces rapid events
when wired into the EventsService cache invalidation pipeline.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.event_subsystem.types import FileEvent, FileEventType
from nexus.system_services.lifecycle.dedup_work_queue import DedupWorkQueue
from nexus.system_services.lifecycle.events_service import EventsService


def _make_event(
    path: str = "/data/file.txt",
    event_type: FileEventType = FileEventType.FILE_WRITE,
    zone_id: str = "root",
    old_path: str | None = None,
) -> FileEvent:
    """Create a FileEvent for testing."""
    return FileEvent(
        type=event_type,
        path=path,
        zone_id=zone_id,
        old_path=old_path,
    )


# ---------------------------------------------------------------------------
# 1. Cache invalidation coalesces rapid writes to same path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_invalidation_coalesces_writes() -> None:
    """10 FILE_WRITE events to the same path → 1 cache invalidation.

    This is the core scenario from Issue #2062 / NEXUS-LEGO-ARCHITECTURE §12.5:
    '10 rapid writes to the same file → 10 events recorded (audit complete)
    but only 1 processing run.'
    """
    # Setup: mock backend (non-passthrough) + mock event bus + mock cache
    backend = MagicMock()
    backend.is_passthrough = False

    mock_cache = MagicMock()
    mock_cache.invalidate_path = MagicMock()

    # Create event bus that yields 10 FILE_WRITE events for the same path
    events = [_make_event(path="/data/file.txt") for _ in range(10)]

    async def mock_subscribe(zone_id: str):  # noqa: ANN202
        for event in events:
            yield event

    event_bus = AsyncMock()
    event_bus.subscribe = mock_subscribe
    event_bus.start = AsyncMock()
    event_bus._started = True

    service = EventsService(
        backend=backend,
        event_bus=event_bus,
        metadata_cache=mock_cache,
    )

    # Start cache invalidation (this creates the dedup queue + worker)
    service._start_cache_invalidation()

    # Wait for events to be processed through the dedup queue
    # The subscribe loop yields all 10 events, but the dedup queue coalesces them
    await asyncio.sleep(0.5)

    # Assert: cache invalidation called at most twice (initial + possible re-queue)
    # The key assertion is that it's significantly less than 10
    invalidation_count = mock_cache.invalidate_path.call_count
    assert invalidation_count <= 2, (
        f"Expected at most 2 invalidations (dedup), got {invalidation_count}"
    )
    assert invalidation_count >= 1, "Expected at least 1 invalidation"

    # Verify the dedup queue metrics
    assert service._dedup_queue is not None
    metrics = service._dedup_queue.metrics
    assert metrics["coalesced"] >= 8, (
        f"Expected at least 8 coalesced events, got {metrics['coalesced']}"
    )

    # Cleanup
    service._stop_cache_invalidation()


# ---------------------------------------------------------------------------
# 2. Different paths are all invalidated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_paths_all_invalidated() -> None:
    """Events for different paths are all processed (no false coalescing)."""
    backend = MagicMock()
    backend.is_passthrough = False

    mock_cache = MagicMock()
    mock_cache.invalidate_path = MagicMock()

    paths = [f"/data/file-{i}.txt" for i in range(5)]
    events = [_make_event(path=p) for p in paths]

    async def mock_subscribe(zone_id: str):  # noqa: ANN202
        for event in events:
            yield event

    event_bus = AsyncMock()
    event_bus.subscribe = mock_subscribe
    event_bus.start = AsyncMock()
    event_bus._started = True

    service = EventsService(
        backend=backend,
        event_bus=event_bus,
        metadata_cache=mock_cache,
    )

    service._start_cache_invalidation()
    await asyncio.sleep(0.5)

    # All 5 different paths should be invalidated
    assert mock_cache.invalidate_path.call_count == 5
    invalidated_paths = sorted(c.args[0] for c in mock_cache.invalidate_path.call_args_list)
    assert invalidated_paths == sorted(paths)

    service._stop_cache_invalidation()


# ---------------------------------------------------------------------------
# 3. Rename events invalidate both old and new paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_event_invalidates_both_paths() -> None:
    """FILE_RENAME event adds both old_path and new path to dedup queue."""
    backend = MagicMock()
    backend.is_passthrough = False

    mock_cache = MagicMock()
    mock_cache.invalidate_path = MagicMock()

    events = [
        _make_event(
            path="/data/new-name.txt",
            event_type=FileEventType.FILE_RENAME,
            old_path="/data/old-name.txt",
        ),
    ]

    async def mock_subscribe(zone_id: str):  # noqa: ANN202
        for event in events:
            yield event

    event_bus = AsyncMock()
    event_bus.subscribe = mock_subscribe
    event_bus.start = AsyncMock()
    event_bus._started = True

    service = EventsService(
        backend=backend,
        event_bus=event_bus,
        metadata_cache=mock_cache,
    )

    service._start_cache_invalidation()
    await asyncio.sleep(0.5)

    # Both old and new paths should be invalidated
    assert mock_cache.invalidate_path.call_count == 2
    invalidated_paths = sorted(c.args[0] for c in mock_cache.invalidate_path.call_args_list)
    assert invalidated_paths == ["/data/new-name.txt", "/data/old-name.txt"]

    service._stop_cache_invalidation()


# ---------------------------------------------------------------------------
# 4. Standalone DedupWorkQueue coalescing with concurrent add/process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_add_during_processing() -> None:
    """Producer adds keys while consumer processes — coalescing still works."""
    q: DedupWorkQueue[str] = DedupWorkQueue()
    processed: list[str] = []

    async def consumer() -> None:
        for _ in range(3):
            key = await asyncio.wait_for(q.get(), timeout=2.0)
            await asyncio.sleep(0.05)  # Simulate work
            processed.append(key)
            q.done(key)

    async def producer() -> None:
        # Burst 1: 5 adds for "a" (should coalesce to 1)
        for _ in range(5):
            await q.add("a")
        await asyncio.sleep(0.15)  # Wait for consumer to process "a"

        # Burst 2: 5 adds for "b" (should coalesce to 1)
        for _ in range(5):
            await q.add("b")
        await asyncio.sleep(0.15)

        # Burst 3: 5 adds for "c" (should coalesce to 1)
        for _ in range(5):
            await q.add("c")

    await asyncio.gather(consumer(), producer())

    assert processed == ["a", "b", "c"]
    assert q.metrics["coalesced"] == 12  # 15 adds - 3 unique = 12 coalesced
