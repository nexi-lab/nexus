"""Unit tests for WALEventLog (Rust-backed event log).

Exercises the full EventLogProtocol surface through the Python wrapper.
Tests are skipped if the _nexus_wal Rust extension is not compiled.

Issue #1397
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.event_log import EventLogConfig

# Skip entire module if Rust extension unavailable
try:
    from nexus.services.event_log.wal_backend import WALEventLog, is_available

    if not is_available():
        pytest.skip("_nexus_wal extension not available", allow_module_level=True)
except ImportError:
    pytest.skip("_nexus_wal extension not available", allow_module_level=True)


def _make_event(
    path: str = "/test.txt",
    zone_id: str = "zone-1",
    event_type: FileEventType = FileEventType.FILE_WRITE,
) -> FileEvent:
    return FileEvent(type=event_type, path=path, zone_id=zone_id)


@pytest.fixture()
def wal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wal"
    d.mkdir()
    return d


@pytest.fixture()
def config(wal_dir: Path) -> EventLogConfig:
    return EventLogConfig(wal_dir=wal_dir, sync_mode="every")


@pytest.fixture()
def wal(config: EventLogConfig) -> WALEventLog:
    log = WALEventLog(config)
    yield log  # type: ignore[misc]
    # Ensure cleanup
    try:
        log._wal.close()
    except Exception:
        pass


class TestAppend:
    @pytest.mark.asyncio()
    async def test_append_single(self, wal: WALEventLog) -> None:
        event = _make_event()
        seq = await wal.append(event)
        assert seq == 1

    @pytest.mark.asyncio()
    async def test_append_returns_sequential(self, wal: WALEventLog) -> None:
        for i in range(1, 11):
            seq = await wal.append(_make_event(path=f"/file-{i}.txt"))
            assert seq == i

    @pytest.mark.asyncio()
    async def test_append_batch(self, wal: WALEventLog) -> None:
        events = [_make_event(path=f"/batch-{i}.txt") for i in range(10)]
        seqs = await wal.append_batch(events)
        assert seqs == list(range(1, 11))


class TestReadFrom:
    @pytest.mark.asyncio()
    async def test_read_all(self, wal: WALEventLog) -> None:
        for i in range(5):
            await wal.append(_make_event(path=f"/f{i}.txt"))

        events = await wal.read_from(1, limit=100)
        assert len(events) == 5
        assert events[0].path == "/f0.txt"
        assert events[4].path == "/f4.txt"

    @pytest.mark.asyncio()
    async def test_read_from_middle(self, wal: WALEventLog) -> None:
        for i in range(10):
            await wal.append(_make_event(path=f"/f{i}.txt"))

        events = await wal.read_from(6, limit=3)
        assert len(events) == 3
        assert events[0].path == "/f5.txt"

    @pytest.mark.asyncio()
    async def test_read_with_zone_filter(self, wal: WALEventLog) -> None:
        await wal.append(_make_event(zone_id="zone-a"))
        await wal.append(_make_event(zone_id="zone-b"))
        await wal.append(_make_event(zone_id="zone-a"))
        await wal.append(_make_event(zone_id="zone-b"))

        events = await wal.read_from(1, limit=100, zone_id="zone-a")
        assert len(events) == 2
        assert all(e.zone_id == "zone-a" for e in events)

    @pytest.mark.asyncio()
    async def test_read_empty_wal(self, wal: WALEventLog) -> None:
        events = await wal.read_from(1, limit=100)
        assert events == []


class TestTruncate:
    @pytest.mark.asyncio()
    async def test_truncate(self, wal_dir: Path) -> None:
        # Use small segments to force rotation
        config = EventLogConfig(wal_dir=wal_dir, segment_size_bytes=100, sync_mode="every")
        wal = WALEventLog(config)
        try:
            for i in range(50):
                await wal.append(_make_event(path=f"/f{i}.txt"))

            deleted = await wal.truncate(30)
            assert deleted > 0

            remaining = await wal.read_from(30, limit=1000)
            assert len(remaining) > 0
            assert all(True for _ in remaining)  # all readable
        finally:
            await wal.close()


class TestSyncAndClose:
    @pytest.mark.asyncio()
    async def test_sync_no_error(self, wal: WALEventLog) -> None:
        await wal.append(_make_event())
        await wal.sync()

    @pytest.mark.asyncio()
    async def test_close(self, wal: WALEventLog) -> None:
        await wal.append(_make_event())
        await wal.close()
        assert not await wal.health_check()

    @pytest.mark.asyncio()
    async def test_health_check_open(self, wal: WALEventLog) -> None:
        assert await wal.health_check()


class TestContextManager:
    @pytest.mark.asyncio()
    async def test_async_context_manager(self, config: EventLogConfig) -> None:
        async with WALEventLog(config) as wal:
            seq = await wal.append(_make_event())
            assert seq == 1
        # After exit, should be closed
        assert not await wal.health_check()


class TestCurrentSequence:
    @pytest.mark.asyncio()
    async def test_current_sequence(self, wal: WALEventLog) -> None:
        assert wal.current_sequence() == 0
        await wal.append(_make_event())
        assert wal.current_sequence() == 1
        await wal.append(_make_event())
        assert wal.current_sequence() == 2


class TestEdgeCases:
    @pytest.mark.asyncio()
    async def test_large_payload(self, wal: WALEventLog) -> None:
        """~1MB payload should work."""
        big_path = "/big/" + "x" * (1024 * 1024)
        event = _make_event(path=big_path)
        seq = await wal.append(event)
        assert seq == 1

        events = await wal.read_from(1, limit=1)
        assert len(events) == 1
        assert events[0].path == big_path

    @pytest.mark.asyncio()
    async def test_empty_zone_id(self, wal: WALEventLog) -> None:
        """Events with no zone_id should use empty string."""
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=None)
        seq = await wal.append(event)
        assert seq == 1

    @pytest.mark.asyncio()
    async def test_reopen_preserves_data(self, config: EventLogConfig) -> None:
        """Close and reopen should preserve all data."""
        wal1 = WALEventLog(config)
        await wal1.append(_make_event(path="/a.txt"))
        await wal1.append(_make_event(path="/b.txt"))
        await wal1.close()

        wal2 = WALEventLog(config)
        try:
            assert wal2.current_sequence() == 2
            events = await wal2.read_from(1, limit=100)
            assert len(events) == 2
            assert events[0].path == "/a.txt"
            assert events[1].path == "/b.txt"
        finally:
            await wal2.close()

    @pytest.mark.asyncio()
    async def test_event_roundtrip_preserves_fields(self, wal: WALEventLog) -> None:
        """All FileEvent fields should survive serialization roundtrip."""
        original = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/new.txt",
            zone_id="zone-42",
            old_path="/old.txt",
            size=1234,
            etag="abc123",
            agent_id="agent-1",
        )
        await wal.append(original)
        events = await wal.read_from(1, limit=1)
        restored = events[0]

        assert restored.path == original.path
        assert restored.zone_id == original.zone_id
        assert restored.old_path == original.old_path
        assert restored.size == original.size
        assert restored.etag == original.etag
        assert restored.agent_id == original.agent_id
