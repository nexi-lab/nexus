"""Unit tests for session_scope context manager (Issue #1254).

TDD: Tests written first, then implementation.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.exc import SQLAlchemyError

from nexus.contracts.exceptions import (
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseTimeoutError,
)
from nexus.storage.session_scope import session_scope


def _make_session_factory():
    """Create a mock session factory that returns a mock session."""
    session = MagicMock()
    factory = MagicMock(return_value=session)
    return factory, session


class TestSessionScopeCommit:
    """Test successful commit path."""

    def test_commits_on_success(self) -> None:
        """session_scope commits when block completes without exception."""
        factory, session = _make_session_factory()

        with session_scope(factory) as s:
            s.add("something")

        session.commit.assert_called_once()
        session.rollback.assert_not_called()
        session.close.assert_called_once()

    def test_yields_session(self) -> None:
        """session_scope yields the session from the factory."""
        factory, session = _make_session_factory()

        with session_scope(factory) as s:
            assert s is session


class TestSessionScopeRollback:
    """Test rollback on exception."""

    def test_rollbacks_on_exception(self) -> None:
        """session_scope rolls back when block raises an exception."""
        factory, session = _make_session_factory()

        with pytest.raises(ValueError, match="oops"), session_scope(factory):
            raise ValueError("oops")

        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        session.close.assert_called_once()

    def test_closes_session_always(self) -> None:
        """session_scope closes session even on exception."""
        factory, session = _make_session_factory()

        with pytest.raises(RuntimeError), session_scope(factory):
            raise RuntimeError("fatal")

        session.close.assert_called_once()


class TestSessionScopeSQLAlchemyTranslation:
    """Test SQLAlchemy error → DatabaseError translation."""

    def test_translates_integrity_error(self) -> None:
        """SAIntegrityError → DatabaseIntegrityError."""
        factory, session = _make_session_factory()

        with pytest.raises(DatabaseIntegrityError) as exc_info, session_scope(factory):
            raise SAIntegrityError("INSERT", {}, Exception("duplicate key"))

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, SAIntegrityError)
        session.rollback.assert_called_once()
        session.close.assert_called_once()

    def test_translates_operational_error_timeout(self) -> None:
        """SAOperationalError with 'timeout' → DatabaseTimeoutError."""
        factory, session = _make_session_factory()

        with pytest.raises(DatabaseTimeoutError), session_scope(factory):
            raise SAOperationalError("SELECT", {}, Exception("connection timeout expired"))

        session.rollback.assert_called_once()

    def test_translates_operational_error_connection(self) -> None:
        """SAOperationalError without 'timeout' → DatabaseConnectionError."""
        factory, session = _make_session_factory()

        with pytest.raises(DatabaseConnectionError), session_scope(factory):
            raise SAOperationalError("SELECT", {}, Exception("could not connect to server"))

        session.rollback.assert_called_once()

    def test_translates_generic_sqlalchemy_error(self) -> None:
        """Generic SQLAlchemyError → DatabaseError."""
        factory, session = _make_session_factory()

        with pytest.raises(DatabaseError), session_scope(factory):
            raise SQLAlchemyError("something went wrong")

        session.rollback.assert_called_once()

    def test_preserves_non_sqlalchemy_exceptions(self) -> None:
        """Non-SQLAlchemy exceptions pass through unmodified."""
        factory, session = _make_session_factory()

        with pytest.raises(KeyError, match="missing"), session_scope(factory):
            raise KeyError("missing")

        session.rollback.assert_called_once()
        session.close.assert_called_once()

    def test_chains_cause_for_all_translations(self) -> None:
        """All SQLAlchemy translations preserve __cause__ for debugging."""
        factory, _session = _make_session_factory()

        # IntegrityError
        with pytest.raises(DatabaseIntegrityError) as exc_info, session_scope(factory):
            raise SAIntegrityError("stmt", {}, Exception("dup"))
        assert isinstance(exc_info.value.__cause__, SAIntegrityError)

        # OperationalError (connection)
        factory2, _session2 = _make_session_factory()
        with pytest.raises(DatabaseConnectionError) as exc_info, session_scope(factory2):
            raise SAOperationalError("stmt", {}, Exception("refused"))
        assert isinstance(exc_info.value.__cause__, SAOperationalError)

        # Generic SQLAlchemy
        factory3, _session3 = _make_session_factory()
        with pytest.raises(DatabaseError) as exc_info, session_scope(factory3):
            raise SQLAlchemyError("generic")
        assert isinstance(exc_info.value.__cause__, SQLAlchemyError)
