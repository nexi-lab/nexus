"""SQLAlchemy-backed UserLookupProtocol implementation.

Wraps the existing user_queries.py functions with session lifecycle
management, satisfying the UserLookupProtocol without exposing
SQLAlchemy sessions to callers.

Issue #2281: Extract Auth/OAuth brick from server/auth.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nexus.bricks.auth.types import UserInfo
from nexus.bricks.auth.user_queries import (
    check_email_available as _check_email,
)
from nexus.bricks.auth.user_queries import (
    check_username_available as _check_username,
)
from nexus.bricks.auth.user_queries import (
    get_user_by_email as _get_by_email,
)
from nexus.bricks.auth.user_queries import (
    get_user_by_id as _get_by_id,
)
from nexus.bricks.auth.user_queries import (
    get_user_by_username as _get_by_username,
)
from nexus.bricks.auth.user_queries import (
    validate_user_uniqueness as _validate_uniqueness,
)

logger = logging.getLogger(__name__)


def _model_to_info(model: Any) -> UserInfo:
    """Convert a UserModel ORM instance to an immutable UserInfo DTO."""
    return UserInfo(
        user_id=model.user_id,
        email=model.email,
        username=model.username,
        display_name=model.display_name,
        avatar_url=model.avatar_url,
        password_hash=model.password_hash,
        primary_auth_method=model.primary_auth_method,
        is_global_admin=model.is_global_admin == 1,
        is_active=model.is_active == 1,
        email_verified=model.email_verified == 1,
        zone_id=getattr(model, "zone_id", None),
        api_key=getattr(model, "api_key", None),
    )


class SQLAlchemyUserLookup:
    """Concrete UserLookupProtocol backed by SQLAlchemy + user_queries.

    Each method opens and closes its own session, so callers never
    handle sessions directly.

    Usage::

        lookup = SQLAlchemyUserLookup(session_factory)
        user = lookup.get_user_by_email("alice@example.com")
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_user_by_email(self, email: str) -> UserInfo | None:
        with self._session_factory() as session:
            model = _get_by_email(session, email)
            return _model_to_info(model) if model else None

    def get_user_by_id(self, user_id: str) -> UserInfo | None:
        with self._session_factory() as session:
            model = _get_by_id(session, user_id)
            return _model_to_info(model) if model else None

    def get_user_by_username(self, username: str) -> UserInfo | None:
        with self._session_factory() as session:
            model = _get_by_username(session, username)
            return _model_to_info(model) if model else None

    def check_email_available(self, email: str) -> bool:
        with self._session_factory() as session:
            return _check_email(session, email)

    def check_username_available(self, username: str) -> bool:
        with self._session_factory() as session:
            return _check_username(session, username)

    def validate_user_uniqueness(
        self,
        email: str | None = None,
        username: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            _validate_uniqueness(session, email=email, username=username)
