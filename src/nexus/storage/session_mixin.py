"""Reusable session context manager for services using SQLAlchemy.

Extracts the repeated _get_session() pattern from AgentRegistry and other
services. Any service that needs session-per-operation semantics can inherit
from SessionMixin and set self._session_factory.

Issue #1355: Created for AgentKeyService, available for future services.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class SessionMixin:
    """Mixin providing a session context manager for SQLAlchemy operations.

    Subclasses must set ``self._session_factory`` to a ``sessionmaker`` instance.
    """

    _session_factory: sessionmaker[Session]

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a session with auto-commit/rollback/close."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
