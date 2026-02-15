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

import sqlite3
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from nexus.services.permissions.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)


class _SQLiteConnHelper:
    """Minimal ConnectionHelper for SQLite testing."""

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        # Create version sequences table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rebac_version_sequences (
                zone_id TEXT PRIMARY KEY,
                current_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
        """)
        self._conn.commit()

    @contextmanager
    def connection(self) -> Any:
        yield self._conn

    def create_cursor(self, conn: Any) -> Any:
        return conn.cursor()

    def fix_sql_placeholders(self, sql: str) -> str:
        return sql  # SQLite uses ? natively


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


@pytest.fixture
def conn_helper():
    """Create a SQLite connection helper."""
    return _SQLiteConnHelper()


class TestIncrementVersionToken:
    """Test increment_version_token function."""

    def test_first_call_returns_v1(self, sqlite_engine, conn_helper):
        token = increment_version_token(sqlite_engine, conn_helper, zone_id="default")
        assert token == "v1"

    def test_second_call_returns_v2(self, sqlite_engine, conn_helper):
        increment_version_token(sqlite_engine, conn_helper, zone_id="default")
        token = increment_version_token(sqlite_engine, conn_helper, zone_id="default")
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
    """Test increment_version_token with PostgreSQL dialect (mocked)."""

    def _make_pg_engine(self):
        """Create a mock engine that reports PostgreSQL dialect."""
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        return engine

    def _make_pg_conn_helper(self, cursor_rows):
        """Create a mock ConnectionHelper for PostgreSQL tests.

        Args:
            cursor_rows: List of rows the cursor should return from fetchone().
                         Each row should be a dict with 'current_version'.
        """
        helper = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()

        # Setup fetchone to return rows in sequence
        mock_cursor.fetchone.side_effect = cursor_rows

        helper.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        helper.connection.return_value.__exit__ = MagicMock(return_value=False)
        helper.create_cursor.return_value = mock_cursor

        return helper, mock_conn, mock_cursor

    def test_postgresql_first_version_returns_v1(self):
        engine = self._make_pg_engine()
        helper, mock_conn, mock_cursor = self._make_pg_conn_helper([{"current_version": 1}])

        token = increment_version_token(engine, helper, zone_id="default")

        assert token == "v1"
        mock_conn.commit.assert_called_once()

    def test_postgresql_executes_upsert_sql(self):
        engine = self._make_pg_engine()
        helper, _, mock_cursor = self._make_pg_conn_helper([{"current_version": 42}])

        token = increment_version_token(engine, helper, zone_id="org_acme")

        assert token == "v42"
        # Verify the SQL contains ON CONFLICT and RETURNING
        executed_sql = mock_cursor.execute.call_args[0][0]
        assert "ON CONFLICT" in executed_sql
        assert "RETURNING current_version" in executed_sql

    def test_postgresql_passes_zone_id_param(self):
        engine = self._make_pg_engine()
        helper, _, mock_cursor = self._make_pg_conn_helper([{"current_version": 1}])

        increment_version_token(engine, helper, zone_id="my_zone")

        # Verify zone_id was passed as parameter
        params = mock_cursor.execute.call_args[0][1]
        assert params == ("my_zone",)

    def test_postgresql_returns_v1_when_fetchone_returns_none(self):
        engine = self._make_pg_engine()
        helper, _, mock_cursor = self._make_pg_conn_helper([None])

        token = increment_version_token(engine, helper, zone_id="default")

        assert token == "v1"

    def test_postgresql_increments_correctly(self):
        engine = self._make_pg_engine()
        helper, _, mock_cursor = self._make_pg_conn_helper(
            [{"current_version": 5}, {"current_version": 6}]
        )

        t1 = increment_version_token(engine, helper, zone_id="zone_a")
        t2 = increment_version_token(engine, helper, zone_id="zone_a")

        assert t1 == "v5"
        assert t2 == "v6"


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
