"""Database connection and session management for Nexus.

.. deprecated:: 0.7.1
    This module is a backward-compatibility shim. All new code should use
    :class:`nexus.storage.record_store.SQLAlchemyRecordStore` directly.
    The module-level functions (``get_session``, ``get_engine``, ``SessionLocal``)
    delegate to a lazily-created ``SQLAlchemyRecordStore`` singleton.

Usage (legacy — prefer RecordStore DI):
    from nexus.storage.database import get_session, get_engine

    with get_session() as session:
        user = session.query(UserModel).filter_by(id=user_id).first()
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from nexus.storage.record_store import SQLAlchemyRecordStore

logger = logging.getLogger(__name__)

_DEPRECATION_MSG = (
    "nexus.storage.database is deprecated and will be removed in v0.8. "
    "Use nexus.storage.record_store.SQLAlchemyRecordStore via dependency injection."
)

# Module-level singleton — lazily created on first call.
_record_store = None


def _get_record_store(
    db_url: str | None = None, db_path: str | Path | None = None
) -> SQLAlchemyRecordStore:
    """Return (and cache) a module-level RecordStore singleton."""
    global _record_store

    if _record_store is not None:
        return _record_store

    from nexus.storage.record_store import SQLAlchemyRecordStore

    _record_store = SQLAlchemyRecordStore(db_url=db_url, db_path=db_path)
    return _record_store


def get_database_url(db_path: str | Path | None = None) -> str:
    """Get database URL from environment or parameter.

    .. deprecated:: 0.7.1
        Use ``SQLAlchemyRecordStore._resolve_db_url()`` instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    from nexus.storage.record_store import SQLAlchemyRecordStore

    return SQLAlchemyRecordStore._resolve_db_url(None, db_path)


def get_engine(db_url: str | None = None, db_path: str | Path | None = None) -> Engine:
    """Get or create the SQLAlchemy engine.

    .. deprecated:: 0.7.1
        Use ``record_store.engine`` instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    store = _get_record_store(db_url, db_path)
    return store.engine


def get_session_factory(
    db_url: str | None = None, db_path: str | Path | None = None
) -> sessionmaker:
    """Get or create the session factory.

    .. deprecated:: 0.7.1
        Use ``record_store.session_factory`` instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    store = _get_record_store(db_url, db_path)
    return store.session_factory


def SessionLocal() -> Session:
    """Create a new database session.

    .. deprecated:: 0.7.1
        Use ``record_store.session_factory()`` instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    store = _get_record_store()
    return store.session_factory()


@contextmanager
def get_session(
    db_url: str | None = None, db_path: str | Path | None = None
) -> Generator[Session, None, None]:
    """Context manager for database sessions.

    Automatically commits on success and rolls back on error.

    .. deprecated:: 0.7.1
        Use ``record_store.session_factory`` with manual session management.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    store = _get_record_store(db_url, db_path)
    session = store.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Reset the global engine and session factory.

    .. deprecated:: 0.7.1
        Use ``record_store.close()`` instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
    global _record_store
    if _record_store is not None:
        _record_store.close()
        _record_store = None
