"""Database-backed username/password authentication with JWT tokens.

This extends LocalAuth to store users in the database (UserModel) instead of in-memory.
Supports user registration, login, password management, and profile updates.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

import bcrypt as bcrypt_lib
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nexus.server.auth.local import LocalAuth
from nexus.storage.models import UserModel

logger = logging.getLogger(__name__)


# ==============================================================================
# User Lookup Helper Functions
# ==============================================================================


def get_user_by_email(session: Session, email: str) -> UserModel | None:
    """Get active user by email.

    Args:
        session: Database session
        email: Email address

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_username(session: Session, username: str) -> UserModel | None:
    """Get active user by username.

    Args:
        session: Database session
        username: Username

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.username == username,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def get_user_by_id(session: Session, user_id: str) -> UserModel | None:
    """Get active user by user ID.

    Args:
        session: Database session
        user_id: User ID

    Returns:
        UserModel or None if not found or inactive
    """
    return session.scalar(
        select(UserModel).where(
            UserModel.user_id == user_id,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )


def check_email_available(session: Session, email: str) -> bool:
    """Check if email is available for registration.

    Only checks active users (soft-deleted users' emails can be reused).

    Args:
        session: Database session
        email: Email to check

    Returns:
        True if email is available (not used by any active user)
    """
    existing = session.scalar(
        select(UserModel).where(
            UserModel.email == email,
            UserModel.is_active == 1,
            UserModel.deleted_at.is_(None),
        )
    )
    return existing is None


def check_username_available(session: Session, username: str) -> bool:
    """Check if username is available for registration.

    Only checks active users (soft-deleted users' usernames can be reused).

    Args:
        session: Database session
        username: Username to check

    Returns:
        True if username is available (not used by any active user)
    """
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

    This is used for SQLite < 3.8.0 where partial indexes are not supported.

    Args:
        session: Database session
        email: Email to check (optional)
        username: Username to check (optional)

    Raises:
        ValueError: If email or username already exists
    """
    if email and not check_email_available(session, email):
        raise ValueError(f"Email {email} already exists")

    if username and not check_username_available(session, username):
        raise ValueError(f"Username {username} already exists")


class DatabaseLocalAuth(LocalAuth):
    """Database-backed local username/password authentication.

    This provider extends LocalAuth to store users in the database (UserModel)
    instead of in-memory, providing persistent user accounts.

    Features:
    - User registration with email/username
    - Password authentication with bcrypt (12+ rounds)
    - JWT token generation and validation
    - Password change functionality
    - User profile management
    - Soft delete support

    Example usage:
        # Create auth provider
        auth = DatabaseLocalAuth(
            session_factory=session_factory,
            jwt_secret="your-secret-key"
        )

        # Register user
        user = auth.register_user(
            email="alice@example.com",
            password="secure-password",
            username="alice"
        )

        # Login
        token = auth.login("alice@example.com", "secure-password")

        # Authenticate with token
        result = await auth.authenticate(token)
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
    ):
        """Initialize database-backed local authentication.

        Args:
            session_factory: SQLAlchemy session factory for database access
            jwt_secret: Secret key for JWT signing. Auto-generated if not provided.
            token_expiry: Token expiration in seconds (default: 3600 = 1 hour)
        """
        # Initialize parent with empty users dict (we'll use database instead)
        super().__init__(jwt_secret=jwt_secret, token_expiry=token_expiry, users={})
        self.session_factory = session_factory

        logger.info("Initialized DatabaseLocalAuth with persistent storage")

    def register_user(
        self,
        email: str,
        password: str,
        username: str | None = None,
        display_name: str | None = None,
        is_admin: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> UserModel:
        """Register a new user account.

        Args:
            email: User email (must be unique among active users)
            password: Plain-text password (will be hashed with bcrypt)
            username: Optional username (must be unique among active users)
            display_name: Optional display name
            is_admin: Whether user is global admin (rare)
            metadata: Optional additional metadata (JSON)

        Note:
            Zone membership is managed via ReBAC groups only.
            Use add_user_to_zone() after registration to assign zone membership.

        Returns:
            Created UserModel

        Raises:
            ValueError: If email or username already exists, or validation fails
        """
        with self.session_factory() as session, session.begin():
            # Validate uniqueness
            validate_user_uniqueness(session, email=email, username=username)

            # Hash password with bcrypt (12 rounds)
            password_bytes = password.encode("utf-8")
            salt = bcrypt_lib.gensalt(rounds=12)
            password_hash = bcrypt_lib.hashpw(password_bytes, salt).decode("utf-8")

            # Generate UUID for user_id
            user_id = str(uuid.uuid4())

            # Create user record
            user = UserModel(
                user_id=user_id,
                email=email,
                username=username,
                display_name=display_name or username or email.split("@")[0],
                password_hash=password_hash,
                primary_auth_method="password",
                is_global_admin=1 if is_admin else 0,
                is_active=1,
                email_verified=0,  # TODO: Implement email verification
                user_metadata=metadata,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )

            session.add(user)
            session.flush()

            # Make instance detached so it can be accessed after session closes
            session.expunge(user)

            logger.info(f"Registered user: {email} (user_id={user_id}, username={username})")

            # TODO: Send verification email
            # send_verification_email(user.email, user.user_id)

            return user

    async def register(
        self,
        email: str,
        password: str,
        username: str | None = None,
        display_name: str | None = None,
    ) -> tuple[UserModel, str]:
        """Register a new user and return user with JWT token.

        This is a convenience method that wraps register_user() and generates a token.
        Used by the /auth/register endpoint.

        Args:
            email: User email
            password: Plain-text password
            username: Optional username
            display_name: Optional display name

        Returns:
            Tuple of (UserModel, JWT token)

        Raises:
            ValueError: If email or username already exists
        """
        # Register user
        user = self.register_user(
            email=email,
            password=password,
            username=username,
            display_name=display_name,
        )

        # Generate JWT token
        user_info = {
            "subject_type": "user",
            "subject_id": user.user_id,
            "zone_id": None,  # TODO: Get from ReBAC groups
            "is_admin": user.is_global_admin == 1,
            "name": user.display_name or user.username or user.email,
        }

        assert user.email is not None, "User email cannot be None after registration"
        token = self.create_token(user.email, user_info)

        return (user, token)

    def login(self, identifier: str, password: str) -> str | None:
        """Authenticate user and create JWT token.

        Args:
            identifier: Email or username
            password: Plain-text password

        Returns:
            JWT token if credentials valid, None otherwise
        """
        with self.session_factory() as session:
            # Try to find user by email or username
            user = get_user_by_email(session, identifier)
            if not user:
                user = get_user_by_username(session, identifier)

            if not user:
                logger.debug(f"Login failed: user not found ({identifier})")
                return None

            # Verify password
            if not user.password_hash:
                logger.debug(f"Login failed: no password set ({identifier})")
                return None

            password_bytes = password.encode("utf-8")
            stored_hash = user.password_hash.encode("utf-8")
            if not bcrypt_lib.checkpw(password_bytes, stored_hash):
                logger.debug(f"Login failed: invalid password ({identifier})")
                return None

            # TODO: Check email_verified before allowing login to sensitive operations
            # if not user.email_verified:
            #     raise ValueError("Email not verified")

            # Update last_login_at (session will autocommit on exit)
            user.last_login_at = datetime.utcnow()
            session.add(user)
            session.commit()

            # Create JWT token
            user_info = {
                "subject_type": "user",
                "subject_id": user.user_id,
                "zone_id": None,  # TODO: Get from ReBAC groups
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
            }

            # Email should always exist for login (verified during user lookup)
            assert user.email is not None, "User email cannot be None during login"
            token = self.create_token(user.email, user_info)
            logger.info(f"Login successful: {identifier} (user_id={user.user_id})")
            return token

    async def login_async(self, identifier: str, password: str) -> tuple[UserModel, str] | None:
        """Authenticate user and return user with JWT token.

        This is a convenience method that wraps login() and returns user object.
        Used by the /auth/login endpoint.

        Args:
            identifier: Email or username
            password: Plain-text password

        Returns:
            Tuple of (UserModel, JWT token) if credentials valid, None otherwise

        Raises:
            ValueError: If credentials are invalid
        """
        token = self.login(identifier, password)
        if not token:
            raise ValueError("Invalid email/username or password")

        # Get user to return
        with self.session_factory() as session:
            user = get_user_by_email(session, identifier)
            if not user:
                user = get_user_by_username(session, identifier)
            if not user:
                raise ValueError("User not found after successful login")

            # Make instance detached so it can be accessed after session closes
            session.expunge(user)
            return (user, token)

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """Change user password.

        Args:
            user_id: User ID
            old_password: Current password (for verification)
            new_password: New password

        Returns:
            True if password changed successfully

        Raises:
            ValueError: If old password is incorrect or user not found
        """
        with self.session_factory() as session, session.begin():
            user = get_user_by_id(session, user_id)
            if not user:
                raise ValueError(f"User not found: {user_id}")

            # Verify old password
            if not user.password_hash:
                raise ValueError("No password set for this user")

            password_bytes = old_password.encode("utf-8")
            stored_hash = user.password_hash.encode("utf-8")
            if not bcrypt_lib.checkpw(password_bytes, stored_hash):
                raise ValueError("Incorrect current password")

            # Hash new password
            new_password_bytes = new_password.encode("utf-8")
            salt = bcrypt_lib.gensalt(rounds=12)
            new_password_hash = bcrypt_lib.hashpw(new_password_bytes, salt).decode("utf-8")

            # Update password
            user.password_hash = new_password_hash
            user.updated_at = datetime.utcnow()
            session.add(user)

            logger.info(f"Password changed for user: {user_id}")
            return True

    def update_profile(
        self,
        user_id: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserModel | None:
        """Update user profile.

        Args:
            user_id: User ID
            display_name: Optional new display name
            avatar_url: Optional new avatar URL
            metadata: Optional metadata to merge

        Returns:
            Updated UserModel or None if not found
        """
        with self.session_factory() as session, session.begin():
            user = get_user_by_id(session, user_id)
            if not user:
                return None

            if display_name is not None:
                user.display_name = display_name

            if avatar_url is not None:
                user.avatar_url = avatar_url

            if metadata is not None:
                import json

                user.user_metadata = json.dumps(metadata)

            user.updated_at = datetime.utcnow()
            session.add(user)
            session.flush()

            # Make instance detached so it can be accessed after session closes
            session.expunge(user)

            logger.info(f"Profile updated for user: {user_id}")
            return user

    def get_user_info(self, user_id: str) -> dict[str, Any] | None:
        """Get user information by user_id.

        Args:
            user_id: User ID

        Returns:
            User info dict or None if not found
        """
        with self.session_factory() as session:
            user = get_user_by_id(session, user_id)
            if not user:
                return None

            return {
                "user_id": user.user_id,
                "email": user.email,
                "username": user.username,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
                "primary_auth_method": user.primary_auth_method,
                "is_global_admin": user.is_global_admin == 1,
                "email_verified": user.email_verified == 1,
                "api_key": user.api_key if hasattr(user, "api_key") else None,
                "zone_id": user.zone_id if hasattr(user, "zone_id") else None,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login_at": (user.last_login_at.isoformat() if user.last_login_at else None),
            }

    def get_user_info_for_jwt(self, user_id: str) -> dict[str, Any] | None:
        """Get user information formatted for JWT token creation.

        Args:
            user_id: User ID

        Returns:
            User info dict formatted for JWT or None if not found
        """
        with self.session_factory() as session:
            user = get_user_by_id(session, user_id)
            if not user:
                return None

            return {
                "subject_type": "user",
                "subject_id": user.user_id,
                "zone_id": user.zone_id,
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
                "api_key": user.api_key,
            }

    # TODO: Implement email verification
    def verify_email(self, user_id: str, verification_token: str) -> bool:
        """Verify user email with token.

        Args:
            user_id: User ID
            verification_token: Verification token from email

        Returns:
            True if email verified successfully

        TODO: Implement verification token generation and validation
        """
        raise NotImplementedError("Email verification not yet implemented")

    # TODO: Implement password reset
    def request_password_reset(self, email: str) -> str | None:
        """Request password reset via email.

        Args:
            email: User email

        Returns:
            Reset token (for testing) or None if user not found

        TODO: Implement password reset token generation and email sending
        """
        raise NotImplementedError("Password reset not yet implemented")

    def reset_password(self, reset_token: str, new_password: str) -> bool:
        """Reset password with reset token.

        Args:
            reset_token: Password reset token
            new_password: New password

        Returns:
            True if password reset successfully

        TODO: Implement password reset token validation and expiry
        """
        raise NotImplementedError("Password reset not yet implemented")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DatabaseLocalAuth":
        """Create from configuration dictionary.

        Args:
            config: Configuration with fields:
                - session_factory: SQLAlchemy session factory
                - jwt_secret: Optional JWT secret key
                - token_expiry: Optional token expiration in seconds

        Returns:
            DatabaseLocalAuth instance
        """
        return cls(
            session_factory=config["session_factory"],
            jwt_secret=config.get("jwt_secret"),
            token_expiry=config.get("token_expiry", 3600),
        )
