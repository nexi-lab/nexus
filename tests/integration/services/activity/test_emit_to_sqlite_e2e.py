"""End-to-end: setup_activity → emit → drained to SQLite segment → queryable."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.emitter import NoopEmitter
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


def _segment_rows(seg_dir: Path, query: str) -> list[tuple]:
    segments = sorted(seg_dir.glob("activity-*.db"))
    assert segments, f"no segment files in {seg_dir}"
    rows: list[tuple] = []
    for seg in segments:
        conn = sqlite3.connect(seg)
        rows.extend(conn.execute(query))
        conn.close()
    return rows


@pytest.mark.asyncio
async def test_emit_to_sqlite_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seg_dir = tmp_path / "segments"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(seg_dir))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")  # disable sweep
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "0")  # CI tmp may be tight
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "1024")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "10")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.01")

    await setup_activity()
    try:
        for i in range(50):
            emit(
                kind=EventKind.SEARCH,
                result=Result.OK,
                actor_token_hash=f"tok{i % 3}",
                subject_zone=f"zone{i % 2}",
                latency_ms=i,
            )
        await asyncio.sleep(0.5)
    finally:
        await shutdown_activity()

    rows = _segment_rows(seg_dir, "SELECT kind, result, subject_zone FROM activity_events")
    assert len(rows) == 50
    assert all(r[0] == "search" and r[1] == "ok" for r in rows)


@pytest.mark.asyncio
async def test_off_loop_emit_then_shutdown_does_not_lose_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An off-loop emit scheduled immediately before shutdown must either
    persist to SQLite or count as dropped — never silently disappear.
    Regression for the call_soon_threadsafe vs worker.stop race."""
    import threading

    seg_dir = tmp_path / "segments"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(seg_dir))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "0")  # CI tmp may be tight

    await setup_activity()
    try:
        from nexus.contracts.protocols.activity import get_emitter

        emitter = get_emitter()
        ready = threading.Event()

        def _emit_off_loop() -> None:
            ready.set()
            emitter.emit(
                kind=EventKind.SEARCH,
                result=Result.OK,
                subject_zone="off-loop",
            )

        t = threading.Thread(target=_emit_off_loop)
        t.start()
        ready.wait()
        t.join()
    finally:
        await shutdown_activity()

    rows = _segment_rows(seg_dir, "SELECT subject_zone FROM activity_events")
    zones = [r[0] for r in rows]
    assert "off-loop" in zones, f"off-loop event missing from durable store; got {zones}"


@pytest.mark.asyncio
async def test_disabled_installs_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    await setup_activity()
    try:
        from nexus.services.activity import get_emitter

        assert isinstance(get_emitter(), NoopEmitter)
    finally:
        await shutdown_activity()


@pytest.mark.asyncio
async def test_stale_legacy_db_unlinked_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-segment activity.db whose newest row is past retention must be
    deleted by the pre-sink sweep DURING setup — the #4336 reclaim path.
    Reclaiming before the sink opens is what lets a boot on a full volume
    succeed instead of degrading into the permanent NoopSink fallback."""
    legacy = tmp_path / "activity.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    conn.execute("INSERT INTO activity_events (id, ts) VALUES ('old', '2020-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(tmp_path / "segments"))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(legacy))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "30")
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "0")  # CI tmp may be tight

    await setup_activity()
    try:
        # No polling: the pre-sink sweep inside setup_activity must have
        # already reclaimed the stale legacy db by the time setup returns.
        assert not legacy.exists()
    finally:
        await shutdown_activity()


@pytest.mark.asyncio
async def test_fresh_legacy_db_stays_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade safety: a legacy db with in-retention rows is neither written
    to nor deleted — new events land in segments only."""
    from datetime import UTC, datetime

    legacy = tmp_path / "activity.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    conn.execute(
        "INSERT INTO activity_events (id, ts) VALUES ('fresh', ?)",
        (datetime.now(tz=UTC).isoformat(),),
    )
    conn.commit()
    conn.close()

    seg_dir = tmp_path / "segments"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(seg_dir))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(legacy))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "30")
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "0")  # CI tmp may be tight
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "5")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.01")

    await setup_activity()
    try:
        for i in range(5):
            emit(kind=EventKind.SEARCH, result=Result.OK, subject_zone=f"z{i}")
        await asyncio.sleep(0.5)
    finally:
        await shutdown_activity()

    assert legacy.exists(), "fresh legacy db must not be deleted"
    conn = sqlite3.connect(legacy)
    legacy_rows = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert legacy_rows == 1, "legacy db must stay frozen (no new writes)"
    rows = _segment_rows(seg_dir, "SELECT id FROM activity_events")
    assert len(rows) == 5
