"""Unit tests for the segmented SQLiteSink (#4336)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.services.activity.events import ActivityEvent, Actor, EventKind, Result, Subject
from nexus.services.activity.sinks.sqlite import SQLiteSink, segment_path


class FakeClock:
    """Mutable UTC clock for rollover tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _clock(iso: str) -> FakeClock:
    return FakeClock(datetime.fromisoformat(iso).replace(tzinfo=UTC))


def _event(
    eid: str,
    *,
    ts: str = "2026-06-09T00:00:00+00:00",
    kind: EventKind = EventKind.SEARCH,
    result: Result = Result.OK,
    latency_ms: int | None = None,
    trace_id: str | None = None,
    actor: Actor | None = None,
    subject: Subject | None = None,
    meta: dict[str, object] | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        id=eid,
        ts=ts,
        kind=kind,
        result=result,
        latency_ms=latency_ms,
        trace_id=trace_id,
        actor=actor if actor is not None else Actor(),
        subject=subject if subject is not None else Subject(),
        meta=meta,
    )


@pytest.mark.asyncio
async def test_schema_bootstrapped_in_todays_segment(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        seg = segment_path(tmp_path, clock.now.date())
        assert seg.name == "activity-2026-06-09.db"
        assert seg.exists()
        conn = sqlite3.connect(seg)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'"
        )
        assert cursor.fetchone() is not None
        idx = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity_events'"
            )
        }
        assert {"idx_ae_ts", "idx_ae_kind_ts", "idx_ae_token_ts", "idx_ae_zone_ts"} <= idx
        conn.close()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_pragmas_applied_including_wal_cap(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        seg = segment_path(tmp_path, clock.now.date())
        conn = sqlite3.connect(seg)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()
        # Per-connection pragmas checked through the sink's own connection.
        assert sink._conn is not None  # noqa: SLF001
        assert sink._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # noqa: SLF001
        assert (
            sink._conn.execute("PRAGMA journal_size_limit").fetchone()[0]  # noqa: SLF001
            == 67108864
        )
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_batch_insert_roundtrip(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        events = [
            _event(
                "1",
                latency_ms=10,
                trace_id="t1",
                actor=Actor(token_hash="aaa", agent="claude", user="alice"),
                subject=Subject(zone="eng", extra={"q": "foo"}),
                meta={"x": 1},
            ),
            _event("2", kind=EventKind.MCP_TOOL_CALL),
        ]
        await sink.write_batch(events)
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    rows = list(
        conn.execute(
            "SELECT id, kind, result, subject_zone, subject_extra, meta "
            "FROM activity_events ORDER BY id"
        )
    )
    conn.close()
    assert rows[0][0] == "1"
    assert rows[0][1] == "search"
    assert rows[0][3] == "eng"
    assert json.loads(rows[0][4]) == {"q": "foo"}
    assert json.loads(rows[0][5]) == {"x": 1}
    assert rows[1][0] == "2"
    assert rows[1][3] is None


@pytest.mark.asyncio
async def test_midnight_rollover_splits_segments(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T23:59:59")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        await sink.write_batch([_event("d2")])
    finally:
        await sink.close()

    seg1 = segment_path(tmp_path, datetime.fromisoformat("2026-06-09").date())
    seg2 = segment_path(tmp_path, datetime.fromisoformat("2026-06-10").date())
    assert seg1.exists() and seg2.exists()

    def _ids(p: Path) -> set[str]:
        conn = sqlite3.connect(p)
        ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
        conn.close()
        return ids

    assert _ids(seg1) == {"d1"}
    assert _ids(seg2) == {"d2"}


@pytest.mark.asyncio
async def test_rollover_removes_old_segments_wal(tmp_path: Path) -> None:
    """Closing the previous segment checkpoints it: its -wal must be gone
    (or empty) so closed segments hold no hidden WAL bytes."""
    clock = _clock("2026-06-09T23:59:59")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        await sink.write_batch([_event("d2")])
        wal1 = Path(f"{segment_path(tmp_path, datetime.fromisoformat('2026-06-09').date())}-wal")
        assert not wal1.exists() or wal1.stat().st_size == 0
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_periodic_checkpoint_truncates_wal(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    # interval 0 → checkpoint after every batch.
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, checkpoint_interval_s=0.0)
    try:
        await sink.write_batch([_event(f"e{i}") for i in range(50)])
        wal = Path(f"{segment_path(tmp_path, clock.now.date())}-wal")
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_open_idempotent_on_existing_segment(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink1 = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink1.write_batch([_event("1")])
    await sink1.close()
    sink2 = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink2.write_batch([_event("2")])
    await sink2.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 2


@pytest.mark.asyncio
async def test_corrupt_todays_segment_raises_at_construction(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    segment_path(tmp_path, clock.now.date()).write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteSink(segment_dir=tmp_path, now_fn=clock)


# ---------------------------------------------------------------------------
# Task 4 — disk-pressure shedding (#4336)
# ---------------------------------------------------------------------------


class FakeDiskUsage:
    """Mutable disk_usage stand-in; .free is the only consumed field."""

    def __init__(self, free: int) -> None:
        self.free = free

    def __call__(self, _path: Path) -> FakeDiskUsage:
        return self


@pytest.mark.asyncio
async def test_shedding_drops_batch_below_min_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=10)
    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=disk,
    )
    try:
        with caplog.at_level("ERROR", logger="nexus.services.activity.sinks.sqlite"):
            await sink.write_batch([_event("1"), _event("2")])
            await sink.write_batch([_event("3")])
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 0  # everything shed
    shed_logs = [r for r in caplog.records if "disk pressure" in r.message]
    assert len(shed_logs) == 1  # edge-triggered: one ERROR, not one per batch


@pytest.mark.asyncio
async def test_shedding_recovers_when_space_returns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=10)
    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=disk,
    )
    try:
        await sink.write_batch([_event("dropped")])
        disk.free = 10 * 1024 * 1024
        sink._free_checked_at = None  # noqa: SLF001 — bypass the 10s statvfs cache
        with caplog.at_level("INFO", logger="nexus.services.activity.sinks.sqlite"):
            await sink.write_batch([_event("kept")])
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
    conn.close()
    assert ids == {"kept"}
    assert any("disk pressure cleared" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_shedding_disabled_with_zero_min_free(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=0)
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, min_free_bytes=0, disk_usage_fn=disk)
    try:
        await sink.write_batch([_event("1")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_statvfs_failure_never_sheds(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")

    def _broken_disk_usage(_path: Path) -> object:
        raise OSError("statvfs unavailable")

    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=_broken_disk_usage,
    )
    try:
        await sink.write_batch([_event("1")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_free_space_check_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.services.activity.sinks.sqlite._FREE_CHECK_INTERVAL_S", 3600.0)
    clock = _clock("2026-06-09T12:00:00")
    calls = {"n": 0}

    class CountingDisk:
        free = 10 * 1024 * 1024

        def __call__(self, _path: Path) -> CountingDisk:
            calls["n"] += 1
            return self

    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=CountingDisk(),
    )
    try:
        await sink.write_batch([_event("1")])
        await sink.write_batch([_event("2")])
        await sink.write_batch([_event("3")])
    finally:
        await sink.close()
    assert calls["n"] == 1  # 10s cache → one statvfs for three batches


@pytest.mark.asyncio
async def test_write_after_close_raises_and_does_not_reopen(tmp_path: Path) -> None:
    """SinkProtocol: a straggler executor write racing close() must fail
    fast, not reopen the segment and leak a connection."""
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink.close()
    with pytest.raises(sqlite3.Error):
        sink._write_rows([("x",) * 12])  # noqa: SLF001 — simulates the straggler
    assert sink._conn is None  # noqa: SLF001 — close() must stay final


@pytest.mark.asyncio
async def test_rollover_failure_drops_batch_then_recovers(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T23:59:59")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        # Tomorrow's segment file pre-corrupted → rollover open fails.
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        bad = segment_path(tmp_path, clock.now.date())
        bad.write_bytes(b"not a sqlite file")
        with pytest.raises(sqlite3.DatabaseError):
            await sink.write_batch([_event("d2")])
        assert sink._conn is None  # noqa: SLF001 — retryable state, no half-open segment
        # Operator clears the bad file -> next batch self-heals.
        bad.unlink()
        await sink.write_batch([_event("d3")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, datetime.fromisoformat("2026-06-10").date()))
    ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
    conn.close()
    assert ids == {"d3"}


@pytest.mark.asyncio
async def test_maintain_rolls_stale_segment_and_releases_handle(tmp_path: Path) -> None:
    """Idle housekeeping: maintain() must close yesterday's handle (so an
    unlinked expired segment actually frees disk) and open today's."""
    clock = _clock("2026-06-09T23:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        clock.now = datetime.fromisoformat("2026-06-10T00:10:00").replace(tzinfo=UTC)
        await sink.maintain()
        assert sink._segment_date == clock.now.date()  # noqa: SLF001
        wal1 = Path(f"{segment_path(tmp_path, datetime.fromisoformat('2026-06-09').date())}-wal")
        assert not wal1.exists() or wal1.stat().st_size == 0
        assert segment_path(tmp_path, clock.now.date()).exists()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_maintain_same_day_keeps_connection(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        before = sink._conn  # noqa: SLF001
        await sink.maintain()
        assert sink._conn is before  # noqa: SLF001
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_maintain_after_close_is_noop(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink.close()
    await sink.maintain()  # must not raise or reopen
    assert sink._conn is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_shedding_releases_stale_segment_without_new_file(tmp_path: Path) -> None:
    """Sustained pressure has no idle ticks: the shed path itself must
    release a previous-day handle (freeing unlinked-segment blocks) and
    must NOT create today's segment on a full volume."""
    clock = _clock("2026-06-09T23:00:00")
    disk = FakeDiskUsage(free=10 * 1024 * 1024)
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, min_free_bytes=1024, disk_usage_fn=disk)
    try:
        await sink.write_batch([_event("d1")])
        disk.free = 10  # pressure arrives together with the date change
        sink._free_checked_at = None  # noqa: SLF001
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        await sink.write_batch([_event("d2")])  # shed
        assert sink._conn is None  # noqa: SLF001 — stale handle closed
        assert not segment_path(tmp_path, clock.now.date()).exists()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_maintain_under_pressure_releases_without_opening(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T23:00:00")
    disk = FakeDiskUsage(free=10 * 1024 * 1024)
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, min_free_bytes=1024, disk_usage_fn=disk)
    try:
        await sink.write_batch([_event("d1")])
        disk.free = 10
        sink._free_checked_at = None  # noqa: SLF001
        clock.now = datetime.fromisoformat("2026-06-10T00:10:00").replace(tzinfo=UTC)
        await sink.maintain()
        assert sink._conn is None  # noqa: SLF001
        assert not segment_path(tmp_path, clock.now.date()).exists()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_defer_open_retries_until_volume_recovers(tmp_path: Path) -> None:
    """defer_open skips the eager open (transient boot failure fallback);
    each batch retries the open until the store becomes writable."""
    clock = _clock("2026-06-09T12:00:00")
    seg = segment_path(tmp_path, clock.now.date())
    seg.write_bytes(b"not a sqlite file")  # simulated transient breakage
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, defer_open=True)  # no raise
    try:
        with pytest.raises(sqlite3.DatabaseError):
            await sink.write_batch([_event("1")])
        seg.unlink()  # volume recovers
        await sink.write_batch([_event("2")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
    conn.close()
    assert ids == {"2"}


@pytest.mark.asyncio
async def test_defer_open_retries_mkdir_until_parent_writable(tmp_path: Path) -> None:
    """mkdir lives in the retry path: a segment dir that could not be
    created at boot (e.g. ENOSPC) is retried on every batch."""
    parent = tmp_path / "ro"
    parent.mkdir()
    seg_dir = parent / "activity"
    parent.chmod(0o555)  # mkdir fails
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=seg_dir, now_fn=clock, defer_open=True)  # no raise
    try:
        with pytest.raises(OSError):
            await sink.write_batch([_event("1")])
        parent.chmod(0o755)  # "volume recovers"
        await sink.write_batch([_event("2")])
    finally:
        parent.chmod(0o755)
        await sink.close()
    conn = sqlite3.connect(segment_path(seg_dir, clock.now.date()))
    ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
    conn.close()
    assert ids == {"2"}
