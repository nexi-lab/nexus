"""Transactional session-scope context manager (Issue #1254).

Replaces repeated try/commit/rollback/finally/close boilerplate with a single
context manager that also translates SQLAlchemy errors to the Nexus
DatabaseError hierarchy at the storage boundary.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.contracts.exceptions import (
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseTimeoutError,
)

# PostgreSQL error codes indicating query timeout / statement timeout.
# See https://www.postgresql.org/docs/current/errcodes-appendix.html
_PG_TIMEOUT_CODES = frozenset(
    {
        "57014",  # query_canceled (statement_timeout triggers this)
        "57P01",  # admin_shutdown (can appear during long queries)
    }
)


def _is_timeout_error(exc: SAOperationalError) -> bool:
    """Determine if an OperationalError represents a timeout.

    Checks the underlying DBAPI error code first (cheap integer/string
    comparison) before falling back to string matching (expensive —
    SQLAlchemy error strings include the full SQL statement).
    """
    # Check PostgreSQL error code via psycopg2/asyncpg .pgcode attribute
    orig = getattr(exc, "orig", None)
    if orig is not None:
        pgcode = getattr(orig, "pgcode", None)
        if pgcode in _PG_TIMEOUT_CODES:
            return True
        # Check for socket.timeout (common in connection timeout scenarios)
        if isinstance(orig, (socket.timeout, TimeoutError)):
            return True
    # Fallback: string matching for SQLite and other dialects
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg


@contextmanager
def session_scope(session_factory: Callable[[], Session]) -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations.

    Commits on success, rolls back on exception, always closes.
    Translates SQLAlchemy errors to Nexus DatabaseError hierarchy.

    Usage:
        with session_scope(self._session_factory) as session:
            session.add(model)
            # auto-commits on exit
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except SAIntegrityError as e:
        session.rollback()
        raise DatabaseIntegrityError(str(e)) from e
    except SAOperationalError as e:
        session.rollback()
        if _is_timeout_error(e):
            raise DatabaseTimeoutError(str(e)) from e
        raise DatabaseConnectionError(str(e)) from e
    except SQLAlchemyError as e:
        session.rollback()
        raise DatabaseError(str(e)) from e
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
