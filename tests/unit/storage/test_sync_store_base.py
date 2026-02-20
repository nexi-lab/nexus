"""Unit tests for SyncStoreBase shared session/dialect logic.

Tests cover:
- __init__: session_factory assignment, is_postgresql config-time flag
- _get_session: session factory present/absent
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


@pytest.fixture
def sqlite_store() -> SyncStoreBase:
    """SyncStoreBase configured for SQLite."""
    session = MagicMock()
    mock_rs = MagicMock()
    mock_rs.session_factory = lambda: session
    return SyncStoreBase(mock_rs, is_postgresql=False)


@pytest.fixture
def pg_store() -> SyncStoreBase:
    """SyncStoreBase configured for PostgreSQL."""
    session = MagicMock()
    mock_rs = MagicMock()
    mock_rs.session_factory = lambda: session
    return SyncStoreBase(mock_rs, is_postgresql=True)


@pytest.fixture
def no_session_store() -> SyncStoreBase:
    """SyncStoreBase with no session factory."""
    return SyncStoreBase(None)


# ===========================================================================
# __init__
# ===========================================================================


class TestInit:
    """Tests for SyncStoreBase initialization."""

    def test_stores_session_factory(self):
        factory = MagicMock()
        mock_rs = MagicMock()
        mock_rs.session_factory = factory
        store = SyncStoreBase(mock_rs)
        assert store._session_factory is factory

    def test_default_dialect_is_sqlite(self):
        store = SyncStoreBase(MagicMock())
        assert store._is_postgres is False

    def test_is_postgresql_flag(self):
        store = SyncStoreBase(MagicMock(), is_postgresql=True)
        assert store._is_postgres is True


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
        mock_rs = MagicMock()
        mock_rs.session_factory = lambda: session
        store = SyncStoreBase(mock_rs)

        with store._with_session() as s:
            s.execute("SELECT 1")

        session.commit.assert_called_once()
        session.close.assert_called_once()

    def test_rollback_on_exception(self):
        """Session should be rolled back on exception."""
        session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.session_factory = lambda: session
        store = SyncStoreBase(mock_rs)

        with pytest.raises(ValueError, match="boom"), store._with_session() as _s:
            raise ValueError("boom")

        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        session.close.assert_called_once()

    def test_close_always_called(self):
        """Session.close() must be called even if commit raises."""
        session = MagicMock()
        session.commit.side_effect = RuntimeError("commit failed")
        mock_rs = MagicMock()
        mock_rs.session_factory = lambda: session
        store = SyncStoreBase(mock_rs)

        with pytest.raises(RuntimeError, match="commit failed"), store._with_session():
            pass  # Success path triggers commit, which fails

        session.close.assert_called_once()

    def test_raises_runtime_error_without_factory(self, no_session_store):
        """No session factory should raise RuntimeError."""
        with (
            pytest.raises(RuntimeError, match="No database session factory available"),
            no_session_store._with_session(),
        ):
            pass

    def test_yields_session_object(self):
        """The yielded object should be the session from the factory."""
        session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.session_factory = lambda: session
        store = SyncStoreBase(mock_rs)

        with store._with_session() as s:
            assert s is session
