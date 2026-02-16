"""Transactional session scope context manager (Issue #1254).

Replaces repeated try/commit/rollback/finally/close boilerplate with a single
context manager that also translates SQLAlchemy errors to Nexus DatabaseError
hierarchy at the storage boundary.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nexus.core.exceptions import (
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseTimeoutError,
)


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
        if "timeout" in str(e).lower():
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
