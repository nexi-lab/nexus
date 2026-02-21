"""Pure user lookup queries for the Auth brick.

Extracted from server/auth/database_local.py (lines 1-70).
These are stateless query functions that only depend on SQLAlchemy.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.storage.models import UserModel


def get_user_by_email(session: Session, email: str) -> UserModel | None:
    """Get active user by email."""
    return session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_username(session: Session, username: str) -> UserModel | None:
    """Get active user by username."""
    return session.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_id(session: Session, user_id: str) -> UserModel | None:
    """Get active user by user ID."""
    return session.scalar(
        select(UserModel).where(
            UserModel.user_id == user_id,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def check_email_available(session: Session, email: str) -> bool:
    """Check if email is available for registration."""
    existing = session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )
    return existing is None


def check_username_available(session: Session, username: str) -> bool:
    """Check if username is available for registration."""
    existing = session.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )
    return existing is None


def validate_user_uniqueness(
    session: Session,
    email: str | None = None,
    username: str | None = None,
) -> None:
    """Validate that email and username are unique among active users.

    Raises:
        ValueError: If email or username already exists.
    """
    if email and not check_email_available(session, email):
        raise ValueError(f"Email {email} already exists")

    if username and not check_username_available(session, username):
        raise ValueError(f"Username {username} already exists")
