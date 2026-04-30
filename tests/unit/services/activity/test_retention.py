"""Unit tests for RetentionTask."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nexus.services.activity.retention import prune_older_than


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    now = datetime.now(tz=UTC)
    rows = [
        (
            "old1",
            (now - timedelta(days=40)).isoformat(),
            "search",
            "ok",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "old2",
            (now - timedelta(days=31)).isoformat(),
            "search",
            "ok",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "new1",
            (now - timedelta(days=10)).isoformat(),
            "search",
            "ok",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        ("new2", now.isoformat(), "search", "ok", None, None, None, None, None, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO activity_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_prune_deletes_rows_older_than_threshold(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    _seed(db)
    deleted = prune_older_than(db_path=db, retention_days=30)
    assert deleted == 2
    conn = sqlite3.connect(db)
    remaining = {row[0] for row in conn.execute("SELECT id FROM activity_events")}
    assert remaining == {"new1", "new2"}
    conn.close()


def test_prune_retention_zero_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    _seed(db)
    deleted = prune_older_than(db_path=db, retention_days=0)
    assert deleted == 0
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    assert count == 4
    conn.close()


def test_prune_handles_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    deleted = prune_older_than(db_path=db, retention_days=30)
    assert deleted == 0
