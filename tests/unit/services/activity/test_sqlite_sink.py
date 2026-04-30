"""Unit tests for SQLiteSink."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity.events import ActivityEvent, Actor, EventKind, Result, Subject
from nexus.services.activity.sinks.sqlite import SQLiteSink


@pytest.mark.asyncio
async def test_schema_bootstrapped_on_open(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        conn = sqlite3.connect(db)
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
        assert "idx_ae_ts" in idx
        assert "idx_ae_kind_ts" in idx
        assert "idx_ae_token_ts" in idx
        assert "idx_ae_zone_ts" in idx
        conn.close()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_pragmas_applied(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        # journal_mode is persistent in the DB file; synchronous is per-connection
        # so we query it through the sink's own connection.
        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()
        assert sink._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_batch_insert_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        events = [
            ActivityEvent(
                id="1",
                ts="2026-04-30T00:00:00Z",
                kind=EventKind.SEARCH,
                result=Result.OK,
                latency_ms=10,
                trace_id="t1",
                actor=Actor(token_hash="aaa", agent="claude", user="alice"),
                subject=Subject(zone="eng", extra={"q": "foo"}),
                meta={"x": 1},
            ),
            ActivityEvent(
                id="2",
                ts="2026-04-30T00:00:01Z",
                kind=EventKind.MCP_TOOL_CALL,
                result=Result.OK,
            ),
        ]
        await sink.write_batch(events)
    finally:
        await sink.close()

    conn = sqlite3.connect(db)
    rows = list(
        conn.execute(
            "SELECT id, kind, result, subject_zone, subject_extra, meta "
            "FROM activity_events ORDER BY id"
        )
    )
    assert rows[0][0] == "1"
    assert rows[0][1] == "search"
    assert rows[0][3] == "eng"
    assert json.loads(rows[0][4]) == {"q": "foo"}
    assert json.loads(rows[0][5]) == {"x": 1}
    assert rows[1][0] == "2"
    assert rows[1][3] is None
    assert rows[1][4] is None
    conn.close()


@pytest.mark.asyncio
async def test_open_idempotent_on_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink1 = SQLiteSink(path=db)
    await sink1.close()
    sink2 = SQLiteSink(path=db)
    await sink2.close()


@pytest.mark.asyncio
async def test_corrupt_file_raises(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    db.write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteSink(path=db)
