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
    deleted by the retention task's first sweep — the #4336 reclaim path."""
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

    await setup_activity()
    try:
        for _ in range(100):  # first sweep runs immediately on start
            if not legacy.exists():
                break
            await asyncio.sleep(0.05)
    finally:
        await shutdown_activity()
    assert not legacy.exists()
