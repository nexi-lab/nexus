"""Unit tests for PgMonitor graceful degradation on SQLite (Issue #762).

PgMonitor methods that rely on pg_stat_statements should return empty /
false values when running against a non-PostgreSQL backend (e.g. SQLite).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.pg_monitor import PgMonitor


@pytest.fixture()
def sqlite_session():
    """In-memory SQLite session (not PostgreSQL)."""
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        yield session


class TestPgMonitorSQLiteDegradation:
    """PgMonitor should degrade gracefully when backed by SQLite."""

    def test_is_postgres_returns_false_for_sqlite(self, sqlite_session: Session) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.is_postgres() is False

    def test_is_pg_stat_statements_enabled_returns_false_for_sqlite(
        self, sqlite_session: Session
    ) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.is_pg_stat_statements_enabled() is False

    def test_get_slowest_queries_returns_empty_when_not_pg(self, sqlite_session: Session) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.get_slowest_queries() == []

    def test_get_most_frequent_queries_returns_empty_when_not_pg(
        self, sqlite_session: Session
    ) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.get_most_frequent_queries() == []

    def test_get_slow_average_queries_returns_empty_when_not_pg(
        self, sqlite_session: Session
    ) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.get_slow_average_queries() == []

    def test_reset_stats_returns_false_when_not_pg(self, sqlite_session: Session) -> None:
        monitor = PgMonitor(sqlite_session)
        assert monitor.reset_stats() is False

    def test_generate_report_for_non_pg(self, sqlite_session: Session) -> None:
        monitor = PgMonitor(sqlite_session)
        report = monitor.generate_report()
        assert report["is_postgres"] is False
        assert report["pg_stat_statements_enabled"] is False
        assert report["slowest_queries"] == []
        assert report["most_frequent_queries"] == []
        assert report["slow_average_queries"] == []
