"""Tests for revision helpers — version tokens and zone revision lookups.

Covers:
- increment_version_token with SQLite (two-step increment)
- increment_version_token monotonic increments
- increment_version_token with PostgreSQL dialect (mocked)
- get_zone_revision_for_grant returns 0 for missing zone
- get_zone_revision_for_grant returns revision when present

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from nexus.rebac.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.storage.models import Base


@pytest.fixture
def sqlite_engine():
    """Create an in-memory SQLite engine with ORM tables.

    Uses StaticPool so all connections (including raw_connection) share
    the same underlying DBAPI connection — required for SQLite :memory:.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def conn_helper(sqlite_engine):
    """Create a TupleRepository as conn_helper for increment_version_token."""
    from nexus.rebac.tuples.repository import TupleRepository

    return TupleRepository(engine=sqlite_engine)


class TestIncrementVersionToken:
    """Test increment_version_token function."""

    def test_first_call_returns_v1(self, sqlite_engine, conn_helper):
        token = increment_version_token(sqlite_engine, conn_helper, zone_id="root")
        assert token == "v1"

    def test_second_call_returns_v2(self, sqlite_engine, conn_helper):
        increment_version_token(sqlite_engine, conn_helper, zone_id="root")
        token = increment_version_token(sqlite_engine, conn_helper, zone_id="root")
        assert token == "v2"

    def test_monotonic_increments(self, sqlite_engine, conn_helper):
        tokens = []
        for _ in range(5):
            tokens.append(increment_version_token(sqlite_engine, conn_helper, zone_id="test_zone"))
        assert tokens == ["v1", "v2", "v3", "v4", "v5"]

    def test_different_zones_independent(self, sqlite_engine, conn_helper):
        t1 = increment_version_token(sqlite_engine, conn_helper, zone_id="zone_a")
        t2 = increment_version_token(sqlite_engine, conn_helper, zone_id="zone_b")
        t3 = increment_version_token(sqlite_engine, conn_helper, zone_id="zone_a")
        assert t1 == "v1"
        assert t2 == "v1"
        assert t3 == "v2"

    def test_default_zone_id(self, sqlite_engine, conn_helper):
        token = increment_version_token(sqlite_engine, conn_helper)
        assert token == "v1"


class TestIncrementVersionTokenPostgres:
    """Test increment_version_token with PostgreSQL dialect (mocked).

    The function uses conn_helper.connection() as a context manager and then
    cursor.execute(sql).fetchone() to get the result. We mock the conn_helper
    and engine to simulate PostgreSQL behavior.
    """

    def _make_pg_setup(self, fetchone_results):
        """Create mock engine + conn_helper that simulates PostgreSQL dialect.

        Args:
            fetchone_results: List of return values for successive
                              cursor.fetchone() calls.
        """
        engine = MagicMock()
        engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_cursor = MagicMock()

        # Build fetchone side effects
        fetchone_values = []
        for row_data in fetchone_results:
            if row_data is None:
                fetchone_values.append(None)
            else:
                fetchone_values.append(row_data)

        mock_cursor.fetchone.side_effect = fetchone_values

        # Build conn_helper mock
        conn_helper = MagicMock()

        @contextmanager
        def mock_connection():
            yield mock_conn

        conn_helper.connection = mock_connection
        conn_helper.create_cursor.return_value = mock_cursor

        return engine, conn_helper, mock_cursor

    def test_postgresql_first_version_returns_v1(self):
        engine, conn_helper, _ = self._make_pg_setup([{"current_version": 1}])

        token = increment_version_token(engine, conn_helper, zone_id="root")

        assert token == "v1"

    def test_postgresql_returns_v1_when_fetchone_returns_none(self):
        engine, conn_helper, _ = self._make_pg_setup([None])

        token = increment_version_token(engine, conn_helper, zone_id="root")

        assert token == "v1"

    def test_postgresql_increments_correctly(self):
        engine1, helper1, _ = self._make_pg_setup([{"current_version": 5}])
        engine2, helper2, _ = self._make_pg_setup([{"current_version": 6}])

        t1 = increment_version_token(engine1, helper1, zone_id="zone_a")
        t2 = increment_version_token(engine2, helper2, zone_id="zone_a")

        assert t1 == "v5"
        assert t2 == "v6"

    def test_postgresql_executes_upsert_sql(self):
        engine, conn_helper, mock_cursor = self._make_pg_setup([{"current_version": 42}])

        token = increment_version_token(engine, conn_helper, zone_id="org_acme")

        assert token == "v42"
        assert mock_cursor.execute.call_count == 1

    def test_postgresql_passes_zone_id_param(self):
        engine, conn_helper, mock_cursor = self._make_pg_setup([{"current_version": 1}])

        increment_version_token(engine, conn_helper, zone_id="my_zone")

        assert mock_cursor.execute.call_count == 1


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
