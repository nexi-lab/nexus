"""UserLookupProtocol — session-free user queries for the Auth brick.

Abstracts SQLAlchemy Session away from user lookup operations so the
Auth brick can be tested with pure in-memory implementations.

Issue #2281: Extract Auth/OAuth brick from server/auth.
"""

from typing import Protocol, runtime_checkable

from nexus.auth.types import UserInfo


@runtime_checkable
class UserLookupProtocol(Protocol):
    """Session-free user lookup interface.

    Concrete implementations manage their own session/connection lifecycle.
    The Auth brick depends only on this protocol, never on SQLAlchemy
    sessions or ORM models directly.
    """

    def get_user_by_email(self, email: str) -> UserInfo | None:
        """Get active user by email.

        Returns:
            UserInfo if found and active, None otherwise.
        """
        ...

    def get_user_by_id(self, user_id: str) -> UserInfo | None:
        """Get active user by user ID.

        Returns:
            UserInfo if found and active, None otherwise.
        """
        ...

    def get_user_by_username(self, username: str) -> UserInfo | None:
        """Get active user by username.

        Returns:
            UserInfo if found and active, None otherwise.
        """
        ...

    def check_email_available(self, email: str) -> bool:
        """Check if email is available for registration."""
        ...

    def check_username_available(self, username: str) -> bool:
        """Check if username is available for registration."""
        ...

    def validate_user_uniqueness(
        self,
        email: str | None = None,
        username: str | None = None,
    ) -> None:
        """Validate that email and username are unique among active users.

        Raises:
            ValueError: If email or username already exists.
        """
        ...
