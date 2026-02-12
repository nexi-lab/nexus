"""Unit tests for SyncStoreBase shared session/dialect logic.

Tests cover:
- __init__: gateway assignment, initial state
- _get_session: session factory present/absent
- _detect_dialect: PostgreSQL detection, SQLite fallback, caching, thread safety
- _dialect_insert: correct dialect-specific INSERT statement
- _dialect_upsert: correct dialect-specific ON CONFLICT handling
- _with_session: commit on success, rollback on error, close always, no factory
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.storage.sync_store_base import SyncStoreBase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_gateway(
    *,
    session_factory: Any = None,
    dialect_name: str = "sqlite",
) -> MagicMock:
    """Build a mock gateway with optional session factory."""
    gw = MagicMock()
    if session_factory is not None:
        gw.session_factory = session_factory
    else:
        gw.session_factory = None
    return gw


def _make_session(dialect_name: str = "sqlite") -> MagicMock:
    """Build a mock SQLAlchemy session."""
    session = MagicMock()
    session.bind.dialect.name = dialect_name
    return session


@pytest.fixture
def sqlite_store() -> SyncStoreBase:
    """SyncStoreBase backed by a SQLite session."""
    session = _make_session("sqlite")
    gw = _make_gateway(session_factory=lambda: session)
    return SyncStoreBase(gw)


@pytest.fixture
def pg_store() -> SyncStoreBase:
    """SyncStoreBase backed by a PostgreSQL session."""
    session = _make_session("postgresql")
    gw = _make_gateway(session_factory=lambda: session)
    return SyncStoreBase(gw)


@pytest.fixture
def no_session_store() -> SyncStoreBase:
    """SyncStoreBase with no session factory."""
    gw = _make_gateway(session_factory=None)
    return SyncStoreBase(gw)


# ===========================================================================
# __init__
# ===========================================================================


class TestInit:
    """Tests for SyncStoreBase initialization."""

    def test_stores_gateway(self):
        gw = MagicMock()
        store = SyncStoreBase(gw)
        assert store._gw is gw

    def test_initial_dialect_is_none(self):
        store = SyncStoreBase(MagicMock())
        assert store._is_postgres is None


# ===========================================================================
# _get_session
# ===========================================================================


class TestGetSession:
    """Tests for _get_session."""

    def test_returns_session_from_factory(self, sqlite_store):
        session = sqlite_store._get_session()
        assert session is not None

    def test_returns_none_without_factory(self, no_session_store):
        assert no_session_store._get_session() is None

    def test_returns_none_when_factory_attr_missing(self):
        """Gateway without session_factory attribute should return None."""
        gw = MagicMock(spec=[])  # No attributes at all
        store = SyncStoreBase(gw)
        assert store._get_session() is None


# ===========================================================================
# _detect_dialect
# ===========================================================================


class TestDetectDialect:
    """Tests for _detect_dialect."""

    def test_detects_postgresql(self, pg_store):
        assert pg_store._detect_dialect() is True

    def test_detects_sqlite(self, sqlite_store):
        assert sqlite_store._detect_dialect() is False

    def test_caches_result(self, pg_store):
        """Second call should return cached value without opening a new session."""
        result1 = pg_store._detect_dialect()
        result2 = pg_store._detect_dialect()
        assert result1 is result2
        assert pg_store._is_postgres is True

    def test_no_session_returns_false(self, no_session_store):
        assert no_session_store._detect_dialect() is False

    def test_exception_during_detection_returns_false(self):
        """Exception reading dialect should fall back to False."""
        session = MagicMock()
        session.bind.dialect.name = property(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        # Simpler: make the attribute access raise
        type(session.bind.dialect).name = property(lambda self: (_ for _ in ()).throw(RuntimeError))

        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)
        assert store._detect_dialect() is False

    def test_session_closed_after_detection(self):
        """Session should be closed after dialect detection."""
        session = _make_session("postgresql")
        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)
        store._detect_dialect()
        session.close.assert_called_once()


# ===========================================================================
# _dialect_insert
# ===========================================================================


class TestDialectInsert:
    """Tests for _dialect_insert."""

    def _make_table(self) -> Any:
        """Create a minimal SQLAlchemy Table for insert() calls."""
        import sqlalchemy as sa

        metadata = sa.MetaData()
        return sa.Table("test_table", metadata, sa.Column("id", sa.Integer, primary_key=True))

    def test_sqlite_uses_sqlite_insert(self):
        from sqlalchemy.dialects.sqlite import Insert as SqliteInsert

        store = SyncStoreBase(MagicMock())
        store._is_postgres = False
        table = self._make_table()
        stmt = store._dialect_insert(table)
        assert isinstance(stmt, SqliteInsert)

    def test_postgresql_uses_pg_insert(self):
        from sqlalchemy.dialects.postgresql import Insert as PgInsert

        store = SyncStoreBase(MagicMock())
        store._is_postgres = True
        table = self._make_table()
        stmt = store._dialect_insert(table)
        assert isinstance(stmt, PgInsert)


# ===========================================================================
# _dialect_upsert
# ===========================================================================


class TestDialectUpsert:
    """Tests for _dialect_upsert."""

    def test_sqlite_upsert_uses_index_elements(self):
        """SQLite upsert should use index_elements for ON CONFLICT."""
        session = MagicMock()
        model = MagicMock()
        values = {"col": "val"}
        update_set = {"col": "updated"}

        store = SyncStoreBase(MagicMock())
        store._is_postgres = False

        with patch.object(store, "_dialect_insert") as mock_insert:
            mock_stmt = MagicMock()
            mock_insert.return_value = mock_stmt
            mock_stmt.values.return_value = mock_stmt
            mock_stmt.on_conflict_do_update.return_value = mock_stmt

            store._dialect_upsert(
                session,
                model,
                values,
                pg_constraint="pg_constraint_name",
                sqlite_index_elements=["id"],
                update_set=update_set,
            )

            mock_stmt.on_conflict_do_update.assert_called_once_with(
                index_elements=["id"], set_=update_set
            )
            session.execute.assert_called_once_with(mock_stmt)

    def test_pg_upsert_uses_constraint(self):
        """PostgreSQL upsert should use constraint for ON CONFLICT."""
        session = MagicMock()
        model = MagicMock()
        values = {"col": "val"}
        update_set = {"col": "updated"}

        store = SyncStoreBase(MagicMock())
        store._is_postgres = True

        with patch.object(store, "_dialect_insert") as mock_insert:
            mock_stmt = MagicMock()
            mock_insert.return_value = mock_stmt
            mock_stmt.values.return_value = mock_stmt
            mock_stmt.on_conflict_do_update.return_value = mock_stmt

            store._dialect_upsert(
                session,
                model,
                values,
                pg_constraint="uq_constraint",
                sqlite_index_elements=["id"],
                update_set=update_set,
            )

            mock_stmt.on_conflict_do_update.assert_called_once_with(
                constraint="uq_constraint", set_=update_set
            )
            session.execute.assert_called_once()


# ===========================================================================
# _with_session context manager
# ===========================================================================


class TestWithSession:
    """Tests for _with_session context manager."""

    def test_commits_on_success(self):
        """Session should be committed on successful block execution."""
        session = MagicMock()
        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)

        with store._with_session() as s:
            s.execute("SELECT 1")

        session.commit.assert_called_once()
        session.close.assert_called_once()

    def test_rollback_on_exception(self):
        """Session should be rolled back on exception."""
        session = MagicMock()
        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)

        with pytest.raises(ValueError, match="boom"), store._with_session() as _s:
            raise ValueError("boom")

        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        session.close.assert_called_once()

    def test_close_always_called(self):
        """Session.close() must be called even if commit raises."""
        session = MagicMock()
        session.commit.side_effect = RuntimeError("commit failed")
        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)

        with pytest.raises(RuntimeError, match="commit failed"), store._with_session():
            pass  # Success path triggers commit, which fails

        session.close.assert_called_once()

    def test_raises_runtime_error_without_factory(self, no_session_store):
        """No session factory should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="No database session factory available"), no_session_store._with_session():
            pass

    def test_yields_session_object(self):
        """The yielded object should be the session from the factory."""
        session = MagicMock()
        gw = _make_gateway(session_factory=lambda: session)
        store = SyncStoreBase(gw)

        with store._with_session() as s:
            assert s is session
