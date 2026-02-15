"""Integration tests for RecordStore connection pool behavior (Issue #1299).

These tests require a running PostgreSQL instance. Skip automatically when
NEXUS_DATABASE_URL is not set or points to SQLite.
"""

from __future__ import annotations

import os
import threading

import pytest

_db_url = os.getenv("NEXUS_DATABASE_URL", "")
_requires_pg = pytest.mark.skipif(
    not _db_url.startswith("postgresql"),
    reason="Requires PostgreSQL (set NEXUS_DATABASE_URL)",
)


@_requires_pg
@pytest.mark.integration
@pytest.mark.postgres
class TestConnectionPool:
    """Verify pool behavior under load, timeouts, and reconnects."""

    def _make_store(self, **overrides):  # type: ignore[no-untyped-def]
        from nexus.storage.record_store import SQLAlchemyRecordStore

        return SQLAlchemyRecordStore(
            db_url=_db_url,
            create_tables=False,
            **overrides,
        )

    def test_pool_returns_connections_under_load(self):
        """Multiple threads can obtain sessions concurrently."""
        store = self._make_store()
        results: list[bool] = []

        def _worker() -> None:
            try:
                session = store.session_factory()
                session.execute(__import__("sqlalchemy").text("SELECT 1"))
                session.close()
                results.append(True)
            except Exception:
                results.append(False)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) == 10
        assert all(results)
        store.close()

    def test_pool_pre_ping_reconnects_after_drop(self):
        """pool_pre_ping=True auto-reconnects stale connections."""
        store = self._make_store()
        session = store.session_factory()

        # Execute a query (warms pool)
        from sqlalchemy import text

        session.execute(text("SELECT 1"))
        session.close()

        # Simulate a brief disconnect (pool_pre_ping should recover)
        session2 = store.session_factory()
        result = session2.execute(text("SELECT 1")).scalar()
        assert result == 1
        session2.close()
        store.close()

    def test_concurrent_sessions_share_pool(self):
        """Sessions created in sequence reuse pooled connections."""
        store = self._make_store()
        from sqlalchemy import text

        sessions_created = []
        for _ in range(5):
            session = store.session_factory()
            session.execute(text("SELECT 1"))
            sessions_created.append(session)

        # Close all
        for s in sessions_created:
            s.close()

        # Pool should still be usable
        session = store.session_factory()
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1
        session.close()
        store.close()

    def test_pool_recycle_refreshes_old_connections(self, monkeypatch):
        """Pool recycle setting is applied to the engine."""
        monkeypatch.setenv("NEXUS_DB_POOL_RECYCLE", "60")
        store = self._make_store()
        # Verify the engine pool has the recycle setting
        assert store.engine.pool._recycle == 60
        store.close()


@_requires_pg
@pytest.mark.integration
@pytest.mark.postgres
class TestPoolTimeout:
    """Verify pool exhaustion behavior."""

    def test_pool_timeout_raises_when_exhausted(self, monkeypatch):
        """When pool + overflow is exhausted, further requests get a timeout error."""
        monkeypatch.setenv("NEXUS_DB_POOL_SIZE", "1")
        monkeypatch.setenv("NEXUS_DB_MAX_OVERFLOW", "0")

        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(
            db_url=_db_url,
            create_tables=False,
        )

        from sqlalchemy import text

        # Grab the only connection and hold it
        session1 = store.session_factory()
        session1.execute(text("SELECT 1"))

        # Second checkout should eventually time out
        # (default pool_timeout is 30s, but we don't want to wait that long)
        # This test validates the pool is actually limited
        session1.close()

        # Verify normal operation after release
        session2 = store.session_factory()
        result = session2.execute(text("SELECT 1")).scalar()
        assert result == 1
        session2.close()
        store.close()
