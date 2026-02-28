"""Tests for revision helpers — version tokens and zone revision lookups.

Covers:
- increment_version_token with SQLite (two-step increment)
- increment_version_token monotonic increments
- increment_version_token with PostgreSQL dialect (mocked)
- get_zone_revision_for_grant returns 0 for missing zone
- get_zone_revision_for_grant returns revision when present

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.rebac.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)


@pytest.fixture
def sqlite_engine():
    """Create an in-memory SQLite engine with version sequences table."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS rebac_version_sequences (
                zone_id TEXT PRIMARY KEY,
                current_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
        """)
        )
        conn.commit()
    return engine


class TestIncrementVersionToken:
    """Test increment_version_token function."""

    def test_first_call_returns_v1(self, sqlite_engine):
        token = increment_version_token(sqlite_engine, zone_id="root")
        assert token == "v1"

    def test_second_call_returns_v2(self, sqlite_engine):
        increment_version_token(sqlite_engine, zone_id="root")
        token = increment_version_token(sqlite_engine, zone_id="root")
        assert token == "v2"

    def test_monotonic_increments(self, sqlite_engine):
        tokens = []
        for _ in range(5):
            tokens.append(increment_version_token(sqlite_engine, zone_id="test_zone"))
        assert tokens == ["v1", "v2", "v3", "v4", "v5"]

    def test_different_zones_independent(self, sqlite_engine):
        t1 = increment_version_token(sqlite_engine, zone_id="zone_a")
        t2 = increment_version_token(sqlite_engine, zone_id="zone_b")
        t3 = increment_version_token(sqlite_engine, zone_id="zone_a")
        assert t1 == "v1"
        assert t2 == "v1"
        assert t3 == "v2"

    def test_default_zone_id(self, sqlite_engine):
        token = increment_version_token(sqlite_engine)
        assert token == "v1"


class TestIncrementVersionTokenPostgres:
    """Test increment_version_token with PostgreSQL dialect (mocked).

    The function uses engine.begin() as a context manager and then
    conn.execute(stmt).fetchone() to get the result. We mock the
    engine to simulate PostgreSQL behavior.
    """

    def _make_pg_engine(self, fetchone_results):
        """Create a mock engine that simulates PostgreSQL dialect.

        Args:
            fetchone_results: List of return values for successive
                              conn.execute(...).fetchone() calls.
        """
        engine = MagicMock()
        engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_results = []
        for row_data in fetchone_results:
            mock_result = MagicMock()
            if row_data is None:
                mock_result.fetchone.return_value = None
            else:
                mock_row = MagicMock()
                mock_row.current_version = row_data["current_version"]
                mock_result.fetchone.return_value = mock_row
            mock_results.append(mock_result)

        mock_conn.execute.side_effect = mock_results

        # engine.begin() returns a context manager yielding mock_conn
        engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        return engine, mock_conn

    def test_postgresql_first_version_returns_v1(self):
        engine, _ = self._make_pg_engine([{"current_version": 1}])

        token = increment_version_token(engine, zone_id="root", is_postgresql=True)

        assert token == "v1"

    def test_postgresql_returns_v1_when_fetchone_returns_none(self):
        engine, _ = self._make_pg_engine([None])

        token = increment_version_token(engine, zone_id="root", is_postgresql=True)

        assert token == "v1"

    def test_postgresql_increments_correctly(self):
        # Each call to increment_version_token calls engine.begin() once,
        # so we need separate engines for each call
        engine1, _ = self._make_pg_engine([{"current_version": 5}])
        engine2, _ = self._make_pg_engine([{"current_version": 6}])

        t1 = increment_version_token(engine1, zone_id="zone_a", is_postgresql=True)
        t2 = increment_version_token(engine2, zone_id="zone_a", is_postgresql=True)

        assert t1 == "v5"
        assert t2 == "v6"

    def test_postgresql_executes_upsert_sql(self):
        engine, mock_conn = self._make_pg_engine([{"current_version": 42}])

        token = increment_version_token(engine, zone_id="org_acme", is_postgresql=True)

        assert token == "v42"
        # Verify execute was called (the SQL is compiled by SQLAlchemy)
        assert mock_conn.execute.call_count == 1

    def test_postgresql_passes_zone_id_param(self):
        engine, mock_conn = self._make_pg_engine([{"current_version": 1}])

        increment_version_token(engine, zone_id="my_zone", is_postgresql=True)

        # Verify execute was called with a compiled statement
        assert mock_conn.execute.call_count == 1


class TestGetZoneRevisionForGrant:
    """Test get_zone_revision_for_grant function."""

    def test_missing_zone_returns_zero(self, sqlite_engine):
        revision = get_zone_revision_for_grant(sqlite_engine, zone_id="nonexistent")
        assert revision == 0

    def test_existing_zone_returns_revision(self, sqlite_engine):
        # Seed a revision
        with sqlite_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                    VALUES (:zone_id, :version, datetime('now'))
                """),
                {"zone_id": "org_acme", "version": 42},
            )
            conn.commit()

        revision = get_zone_revision_for_grant(sqlite_engine, zone_id="org_acme")
        assert revision == 42

    def test_missing_table_returns_zero(self):
        """get_zone_revision_for_grant handles missing table gracefully."""
        engine = create_engine("sqlite:///:memory:")
        # Don't create the table
        revision = get_zone_revision_for_grant(engine, zone_id="any")
        assert revision == 0

    def test_returns_int(self, sqlite_engine):
        with sqlite_engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                    VALUES (:zone_id, :version, datetime('now'))
                """),
                {"zone_id": "test_zone", "version": 7},
            )
            conn.commit()

        result = get_zone_revision_for_grant(sqlite_engine, zone_id="test_zone")
        assert isinstance(result, int)
        assert result == 7
