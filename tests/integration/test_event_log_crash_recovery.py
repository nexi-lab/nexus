"""Integration tests for WAL crash recovery.

Simulates crash scenarios by manipulating segment files between
WAL close and reopen. Verifies that the recovery logic correctly
handles truncated records and corrupted CRC bytes.

Issue #1397
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.core.protocols.event_log import EventLogConfig

try:
    from nexus.core.event_log_wal import WALEventLog, is_available

    if not is_available():
        pytest.skip("_nexus_wal extension not available", allow_module_level=True)
except ImportError:
    pytest.skip("_nexus_wal extension not available", allow_module_level=True)


def _make_event(path: str = "/test.txt") -> FileEvent:
    return FileEvent(type=FileEventType.FILE_WRITE, path=path, zone_id="z1")


def _find_segments(wal_dir: Path) -> list[Path]:
    return sorted(wal_dir.glob("wal-*.seg"))


@pytest.fixture()
def wal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wal"
    d.mkdir()
    return d


class TestCleanRecovery:
    @pytest.mark.asyncio()
    async def test_reopen_all_events_readable(self, wal_dir: Path) -> None:
        """Write N events, close, reopen — all N should be readable."""
        config = EventLogConfig(wal_dir=wal_dir, sync_mode="every")
        n = 20

        wal = WALEventLog(config)
        for i in range(n):
            await wal.append(_make_event(path=f"/f{i}.txt"))
        await wal.close()

        wal2 = WALEventLog(config)
        try:
            events = await wal2.read_from(1, limit=n + 10)
            assert len(events) == n
            for i, e in enumerate(events):
                assert e.path == f"/f{i}.txt"
        finally:
            await wal2.close()


class TestTruncatedRecovery:
    @pytest.mark.asyncio()
    async def test_truncated_tail_recovered(self, wal_dir: Path) -> None:
        """Simulate crash mid-write: truncate last segment at various offsets."""
        config = EventLogConfig(wal_dir=wal_dir, sync_mode="every")

        wal = WALEventLog(config)
        for i in range(10):
            await wal.append(_make_event(path=f"/f{i}.txt"))
        await wal.close()

        # Find last segment, append garbage to simulate partial write
        segments = _find_segments(wal_dir)
        last_seg = segments[-1]
        original_size = last_seg.stat().st_size

        with last_seg.open("ab") as f:
            f.write(b"\xde\xad\xbe\xef\x00\x01\x02\x03")

        # Reopen — recovery should truncate the garbage
        wal2 = WALEventLog(config)
        try:
            events = await wal2.read_from(1, limit=100)
            assert len(events) == 10
            # Segment should be back to original size
            assert last_seg.stat().st_size == original_size
        finally:
            await wal2.close()


class TestCorruptedCrcRecovery:
    @pytest.mark.asyncio()
    async def test_corrupted_crc_truncated(self, wal_dir: Path) -> None:
        """Corrupt the CRC of a record — recovery should truncate at that point."""
        config = EventLogConfig(wal_dir=wal_dir, sync_mode="every")

        wal = WALEventLog(config)
        for i in range(5):
            await wal.append(_make_event(path=f"/f{i}.txt"))
        await wal.close()

        segments = _find_segments(wal_dir)
        last_seg = segments[-1]
        data = bytearray(last_seg.read_bytes())

        # Corrupt the CRC of the last record (last 4 bytes before EOF)
        if len(data) >= 4:
            data[-4] ^= 0xFF
            last_seg.write_bytes(bytes(data))

        wal2 = WALEventLog(config)
        try:
            events = await wal2.read_from(1, limit=100)
            # At least the first 4 records should survive (5th was corrupted)
            assert len(events) >= 4
            assert len(events) < 5
        finally:
            await wal2.close()

    @pytest.mark.asyncio()
    async def test_corrupted_mid_segment(self, wal_dir: Path) -> None:
        """Corrupt a CRC in the middle of a segment."""
        config = EventLogConfig(wal_dir=wal_dir, sync_mode="every")

        wal = WALEventLog(config)
        for i in range(5):
            await wal.append(_make_event(path=f"/f{i}.txt"))
        await wal.close()

        segments = _find_segments(wal_dir)
        last_seg = segments[-1]
        data = bytearray(last_seg.read_bytes())

        # Find and corrupt the CRC of the 3rd record
        # Each record: seq(8) + zid_len(2) + zid(2 "z1") + plen(4) + payload + crc(4)
        # Skip header (8 bytes) and first 2 records to get to the 3rd
        offset = 8  # header
        for _ in range(2):
            # Read zone_id_len at offset+8
            zid_len = struct.unpack_from("<H", data, offset + 8)[0]
            # Read payload_len at offset+8+2+zid_len
            plen = struct.unpack_from("<I", data, offset + 10 + zid_len)[0]
            # Total record size: 8+2+zid_len+4+plen+4
            offset += 8 + 2 + zid_len + 4 + plen + 4

        # Now `offset` points to the start of the 3rd record
        # Skip to CRC: 8+2+zid_len+4+plen bytes in
        zid_len_3 = struct.unpack_from("<H", data, offset + 8)[0]
        plen_3 = struct.unpack_from("<I", data, offset + 10 + zid_len_3)[0]
        crc_offset = offset + 8 + 2 + zid_len_3 + 4 + plen_3
        data[crc_offset] ^= 0xFF
        last_seg.write_bytes(bytes(data))

        wal2 = WALEventLog(config)
        try:
            events = await wal2.read_from(1, limit=100)
            # First 2 records should survive, 3rd+ truncated
            assert len(events) == 2
        finally:
            await wal2.close()
