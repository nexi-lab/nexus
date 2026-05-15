"""End-to-end: setup_activity → emit → drained to SQLite → queryable."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.emitter import NoopEmitter
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


@pytest.mark.asyncio
async def test_emit_to_sqlite_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")  # disable prune
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

    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT kind, result, subject_zone FROM activity_events"))
    conn.close()
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

    db = tmp_path / "activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
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

    # The event must be in the durable store (the QueueEmitter quiesce
    # step in shutdown_activity guarantees it).
    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT subject_zone FROM activity_events"))
    conn.close()
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
