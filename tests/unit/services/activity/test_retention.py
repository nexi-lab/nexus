"""Unit tests for segment-based retention (#4336)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.services.activity.retention import RetentionTask, sweep_expired

_NOW = datetime(2026, 6, 9, 18, 0, 0, tzinfo=UTC)


def _now_fn() -> datetime:
    return _NOW


def _make_db(path: Path, ts_values: list[str] | None = None) -> None:
    """Create a minimal activity_events db with optional rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    for i, ts in enumerate(ts_values or []):
        conn.execute("INSERT INTO activity_events (id, ts) VALUES (?, ?)", (f"r{i}", ts))
    conn.commit()
    conn.close()


def _make_segment(seg_dir: Path, day: str) -> Path:
    path = seg_dir / f"activity-{day}.db"
    _make_db(path, [f"{day}T12:00:00+00:00"])
    return path


def test_sweep_unlinks_expired_segments_and_siblings(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2026-05-01")
    Path(f"{old}-wal").write_bytes(b"wal")
    Path(f"{old}-shm").write_bytes(b"shm")
    boundary = _make_segment(seg_dir, "2026-05-10")  # cutoff date itself: kept
    fresh = _make_segment(seg_dir, "2026-06-08")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 1
    assert not old.exists()
    assert not Path(f"{old}-wal").exists()
    assert not Path(f"{old}-shm").exists()
    assert boundary.exists()  # may still hold rows newer than cutoff ts
    assert fresh.exists()


def test_sweep_retention_zero_is_noop(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2020-01-01")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=0, now_fn=_now_fn
    )
    assert deleted == 0
    assert old.exists()


def test_sweep_missing_dir_is_noop(tmp_path: Path) -> None:
    deleted = sweep_expired(
        segment_dir=tmp_path / "nope",
        legacy_db_path=None,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 0


def test_sweep_skips_non_segment_filenames(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    seg_dir.mkdir()
    odd = seg_dir / "activity-notadate.db"
    odd.write_bytes(b"")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 0
    assert odd.exists()


def test_legacy_unlinked_when_fully_stale(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"])
    Path(f"{legacy}-wal").write_bytes(b"wal")
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 1
    assert not legacy.exists()
    assert not Path(f"{legacy}-wal").exists()


def test_legacy_kept_while_fresh_rows_remain(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-04-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"])
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 0
    assert legacy.exists()


def test_legacy_empty_table_unlinked(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, [])
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 1
    assert not legacy.exists()


def test_legacy_corrupt_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = tmp_path / "activity.db"
    legacy.write_bytes(b"not a sqlite file")
    with caplog.at_level("WARNING"):
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
        )
    assert deleted == 0
    assert legacy.exists()
    assert any("unreadable" in r.message for r in caplog.records)


def test_legacy_missing_table_refused_not_deleted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-safe: a SQLite db without activity_events (e.g. a mispointed
    NEXUS_ACTIVITY_DB_PATH at another subsystem's db) must never be
    unlinked — it cannot be positively identified as a legacy store."""
    legacy = tmp_path / "activity.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    with caplog.at_level("WARNING"):
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
        )
    assert deleted == 0
    assert legacy.exists()
    assert any("no activity_events table" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_retention_task_zero_disables_start(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=0,
        interval_s=0.01,
    )
    await task.start()
    assert task._task is None  # noqa: SLF001
    await task.stop()


@pytest.mark.asyncio
async def test_retention_task_runs_and_deletes(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2020-01-01")
    fresh_day = datetime.now(tz=UTC).date().isoformat()
    fresh = _make_segment(seg_dir, fresh_day)
    task = RetentionTask(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, interval_s=0.05
    )
    await task.start()
    # Poll instead of fixed sleep — under xdist load the task body may not
    # run within a small hard-coded window.
    for _ in range(200):
        if task.total_deleted >= 1:
            break
        await asyncio.sleep(0.02)
    await task.stop()
    assert task.total_deleted == 1
    assert not old.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_retention_task_stop_before_start_is_safe(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=30,
        interval_s=0.01,
    )
    await task.stop()  # must not raise


@pytest.mark.asyncio
async def test_retention_task_double_start_is_idempotent(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=30,
        interval_s=0.01,
    )
    await task.start()
    first_task = task._task  # noqa: SLF001
    await task.start()
    assert task._task is first_task  # noqa: SLF001
    await task.stop()


def test_sweep_skips_regex_shaped_but_invalid_date(tmp_path: Path) -> None:
    """activity-2026-13-99.db matches the segment regex but is not a date."""
    seg_dir = tmp_path / "activity"
    seg_dir.mkdir()
    odd = seg_dir / "activity-2026-13-99.db"
    odd.write_bytes(b"")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 0
    assert odd.exists()


def test_legacy_path_with_hash_char_not_misread_as_expired(tmp_path: Path) -> None:
    """Regression: '#' in the path must not truncate the SQLite URI — a
    fresh legacy db behind such a path was misread as expired and deleted."""
    legacy_dir = tmp_path / "data#prod"
    legacy = legacy_dir / "activity.db"
    _make_db(legacy, ["2026-06-01T00:00:00+00:00"])  # fresh row, within retention
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 0
    assert legacy.exists()


def test_sweep_increments_segments_deleted_counter(tmp_path: Path) -> None:
    from prometheus_client import REGISTRY

    def _value() -> float:
        return REGISTRY.get_sample_value("nexus_activity_segments_deleted_total") or 0.0

    seg_dir = tmp_path / "activity"
    _make_segment(seg_dir, "2026-05-01")
    _make_segment(seg_dir, "2026-05-02")
    before = _value()
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 2
    assert _value() == before + 2


def test_legacy_probe_cached_across_sweeps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Frozen legacy db: max(ts) is probed once; later sweeps answer from the
    cache instead of re-paying a full-WAL-recovery read-only open."""
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-06-01T00:00:00+00:00"])  # fresh row → kept
    cache: dict[str, str] = {}
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
        legacy_probe_cache=cache,
    )
    assert deleted == 0
    assert cache  # populated by the first successful probe
    # Corrupt the file: a re-probe would log "unreadable"; the cache must
    # answer instead.
    legacy.write_bytes(b"not a sqlite file")
    with caplog.at_level("WARNING"):
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
            legacy_probe_cache=cache,
        )
    assert deleted == 0
    assert not any("unreadable" in r.message for r in caplog.records)
    # A stale cached max(ts) is only a deletion CANDIDATE: the sweep must
    # re-validate with a fresh probe, and the (still corrupt) file is kept.
    with caplog.at_level("WARNING"):
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=lambda: datetime(2026, 8, 1, tzinfo=UTC),
            legacy_probe_cache=cache,
        )
    assert deleted == 0
    assert legacy.exists()
    assert any("unreadable" in r.message for r in caplog.records)


def test_legacy_stale_cache_revalidated_then_deleted(tmp_path: Path) -> None:
    """The deletion path re-probes: a genuinely stale legacy db is removed."""
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-06-01T00:00:00+00:00"])
    cache: dict[str, str] = {}
    assert (
        sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
            legacy_probe_cache=cache,
        )
        == 0
    )
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=lambda: datetime(2026, 8, 1, tzinfo=UTC),
        legacy_probe_cache=cache,
    )
    assert deleted == 1
    assert not legacy.exists()


def test_legacy_concurrent_writer_rows_block_deletion(tmp_path: Path) -> None:
    """Rolling-upgrade safety: rows appended by an old pre-segment writer
    AFTER the cache probe must block deletion (re-probe sees them)."""
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-06-01T00:00:00+00:00"])
    cache: dict[str, str] = {}
    sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
        legacy_probe_cache=cache,
    )
    assert cache  # max(ts)=2026-06-01 cached
    # Old writer appends a FRESH row after the probe.
    conn = sqlite3.connect(legacy)
    conn.execute(
        "INSERT INTO activity_events (id, ts) VALUES ('late', '2026-07-30T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    # Cutoff passes the CACHED ts (2026-06-01) but not the late row.
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=lambda: datetime(2026, 7, 15, tzinfo=UTC),
        legacy_probe_cache=cache,
    )
    assert deleted == 0
    assert legacy.exists()


def test_legacy_locked_by_active_writer_kept(tmp_path: Path) -> None:
    """Rolling-upgrade fence: a pre-segment writer holding the write lock
    blocks deletion — the BEGIN IMMEDIATE probe treats busy as evidence of
    an active writer and keeps the file for a later tick."""
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2020-01-01T00:00:00+00:00"])  # stale rows
    holder = sqlite3.connect(legacy)
    holder.execute("BEGIN IMMEDIATE")  # old writer mid-transaction
    try:
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
        )
    finally:
        holder.rollback()
        holder.close()
    assert deleted == 0
    assert legacy.exists()
