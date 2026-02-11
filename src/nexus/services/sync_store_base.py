"""Base class for sync-related database stores.

Provides shared session management and dialect detection for
ChangeLogStore, SyncBacklogStore, and future sync stores.

Extracted from ChangeLogStore (Issue #1127) during Phase 0 refactoring
for Issue #1129 (Bidirectional Sync).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


class SyncStoreBase:
    """Base class providing session management and dialect detection.

    Subclasses (ChangeLogStore, SyncBacklogStore) inherit:
    - _with_session(): Context manager for DB sessions (commit/rollback/close)
    - _detect_dialect(): Cached, thread-safe PostgreSQL detection
    """

    def __init__(self, gateway: NexusFSGateway) -> None:
        """Initialize with gateway for database access.

        Args:
            gateway: NexusFSGateway for session factory access
        """
        self._gw = gateway
        self._is_postgres: bool | None = None
        self._dialect_lock = threading.Lock()

    def _get_session(self) -> Any:
        """Get a database session from the gateway."""
        if hasattr(self._gw, "session_factory") and self._gw.session_factory is not None:
            return self._gw.session_factory()
        return None

    def _detect_dialect(self) -> bool:
        """Detect if the database is PostgreSQL. Result is cached after first call.

        Thread-safe: uses double-checked locking to avoid races.

        Returns:
            True if PostgreSQL, False for SQLite or unknown
        """
        if self._is_postgres is not None:
            return self._is_postgres
        with self._dialect_lock:
            if self._is_postgres is not None:
                return self._is_postgres
            session = self._get_session()
            if session is not None:
                try:
                    self._is_postgres = session.bind.dialect.name == "postgresql"
                except Exception:
                    self._is_postgres = False
                finally:
                    session.close()
            else:
                self._is_postgres = False
            return self._is_postgres

    @contextmanager
    def _with_session(self) -> Generator[Any, None, None]:
        """Context manager for database sessions.

        Handles commit on success, rollback on error, and close on exit.

        Yields:
            SQLAlchemy session

        Raises:
            RuntimeError: If no session factory is available
        """
        session = self._get_session()
        if session is None:
            raise RuntimeError("No database session factory available")
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
