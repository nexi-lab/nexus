"""SQLAlchemy implementation of UserStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import UserDTO
from nexus.storage.models import UserModel

logger = logging.getLogger(__name__)


def _to_dto(user: UserModel) -> UserDTO:
    return UserDTO(
        user_id=user.user_id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        is_active=bool(user.is_active),
        email_verified=bool(user.email_verified),
        zone_id=user.zone_id,
        avatar_url=user.avatar_url,
        user_metadata=user.user_metadata,
        password_hash=user.password_hash,
        primary_auth_method=user.primary_auth_method,
        is_global_admin=bool(user.is_global_admin),
        api_key=None,  # Never expose plaintext legacy api_key; use hashed api_keys table
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


_ACTIVE_FILTERS = (UserModel.is_active == 1, UserModel.deleted_at.is_(None))


class SQLAlchemyUserStore:
    """UserStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create_user(
        self,
        *,
        user_id: str,
        email: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        primary_auth_method: str = "password",
        is_admin: bool = False,
        email_verified: bool = False,
        zone_id: str | None = None,
        user_metadata: str | None = None,
    ) -> UserDTO:
        now = datetime.now(UTC)
        user = UserModel(
            user_id=user_id,
            email=email,
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            primary_auth_method=primary_auth_method,
            is_global_admin=1 if is_admin else 0,
            is_active=1,
            email_verified=1 if email_verified else 0,
            zone_id=zone_id,
            user_metadata=user_metadata,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(user)
            session.commit()
            session.refresh(user)
            return _to_dto(user)

    def get_by_id(self, user_id: str) -> UserDTO | None:
        with self._session_factory() as session:
            user = session.scalar(
                select(UserModel).where(UserModel.user_id == user_id, *_ACTIVE_FILTERS)
            )
            return _to_dto(user) if user else None

    def get_by_email(self, email: str) -> UserDTO | None:
        with self._session_factory() as session:
            user = session.scalar(
                select(UserModel).where(UserModel.email == email, *_ACTIVE_FILTERS)
            )
            return _to_dto(user) if user else None

    def get_by_username(self, username: str) -> UserDTO | None:
        with self._session_factory() as session:
            user = session.scalar(
                select(UserModel).where(UserModel.username == username, *_ACTIVE_FILTERS)
            )
            return _to_dto(user) if user else None

    def update_user(self, user_id: str, **fields: object) -> UserDTO | None:
        with self._session_factory() as session:
            user = session.scalar(
                select(UserModel).where(UserModel.user_id == user_id, *_ACTIVE_FILTERS)
            )
            if not user:
                return None
            for key, value in fields.items():
                if hasattr(user, key):
                    setattr(user, key, value)
            user.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(user)
            return _to_dto(user)

    def check_email_available(self, email: str) -> bool:
        return self.get_by_email(email) is None

    def check_username_available(self, username: str) -> bool:
        return self.get_by_username(username) is None
