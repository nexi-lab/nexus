"""Base class for sync-related database stores.

Provides shared session management and dialect-aware helpers for
ChangeLogStore and future sync stores.

Extracted from ChangeLogStore (Issue #1127).
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class SyncStoreBase:
    """Base class providing session management and dialect-aware helpers.

    Subclasses (ChangeLogStore) inherit:
    - _with_session(): Context manager for DB sessions (commit/rollback/close)
    - _dialect_insert(): Dialect-appropriate INSERT statement
    - _dialect_upsert(): Dialect-aware INSERT ON CONFLICT DO UPDATE
    """

    def __init__(
        self,
        record_store: "RecordStoreABC | None",
        *,
        is_postgresql: bool = False,
    ) -> None:
        """Initialize with a record store for database access.

        Args:
            record_store: RecordStoreABC instance providing session factory.
            is_postgresql: Whether the database is PostgreSQL (config-time flag).
                Determines dialect-specific INSERT/UPSERT behaviour.
        """
        self._session_factory = record_store.session_factory if record_store else None
        self._is_postgres: bool = is_postgresql

    def _get_session(self) -> Any:
        """Get a database session from the session factory."""
        if self._session_factory is not None:
            return self._session_factory()
        return None

    def _dialect_insert(self, model: type) -> Any:
        """Return dialect-appropriate INSERT statement for the given model.

        Centralizes dialect-specific imports so business logic doesn't need them.

        Args:
            model: SQLAlchemy model class

        Returns:
            Dialect-appropriate insert statement (pg_insert or sqlite_insert)
        """
        if self._is_postgres:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            return pg_insert(model)
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(model)

    def _dialect_upsert(
        self,
        session: Any,
        model: type,
        values: dict[str, Any],
        *,
        pg_constraint: str,
        sqlite_index_elements: list[str],
        update_set: dict[str, Any],
    ) -> None:
        """Dialect-aware single-row INSERT ON CONFLICT DO UPDATE.

        Centralizes pg_insert/sqlite_insert so subclasses don't need dialect imports.

        Args:
            session: SQLAlchemy session
            model: SQLAlchemy model class
            values: Column values for INSERT
            pg_constraint: PostgreSQL constraint name for ON CONFLICT
            sqlite_index_elements: SQLite index columns for ON CONFLICT
            update_set: Columns to update on conflict
        """
        stmt = self._dialect_insert(model).values(**values)
        if self._is_postgres:
            stmt = stmt.on_conflict_do_update(constraint=pg_constraint, set_=update_set)
        else:
            stmt = stmt.on_conflict_do_update(index_elements=sqlite_index_elements, set_=update_set)
        session.execute(stmt)

    @contextmanager
    def _with_session(self) -> Generator[Any, None, None]:
        """Context manager for database sessions.

        Handles commit on success, rollback on error, and close on exit.
        Translates SQLAlchemy errors to Nexus DatabaseError hierarchy.

        Yields:
            SQLAlchemy session

        Raises:
            RuntimeError: If no session factory is available
            DatabaseError: On SQLAlchemy failures (translated from SA errors)
        """
        if self._session_factory is None:
            raise RuntimeError("No database session factory available")

        from nexus.storage.session_scope import session_scope

        with session_scope(self._session_factory) as session:
            yield session
