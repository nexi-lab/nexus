"""Database-backed username/password authentication with JWT tokens.

This extends LocalAuth to store users in the database (UserModel) instead of in-memory.
Supports user registration, login, password management, and profile updates.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import bcrypt as bcrypt_lib
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nexus.server.auth.base import AuthResult
from nexus.server.auth.local import LocalAuth
from nexus.server.auth.user_helpers import (
    check_email_available,
    check_username_available,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    validate_user_uniqueness,
)
from nexus.storage.models import UserModel

logger = logging.getLogger(__name__)


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
        tenant_id: str | None = None,
        is_admin: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> UserModel:
        """Register a new user account.

        Args:
            email: User email (must be unique among active users)
            password: Plain-text password (will be hashed with bcrypt)
            username: Optional username (must be unique among active users)
            display_name: Optional display name
            tenant_id: Optional tenant ID (for metadata, actual membership via ReBAC)
            is_admin: Whether user is global admin (rare)
            metadata: Optional additional metadata (JSON)

        Returns:
            Created UserModel

        Raises:
            ValueError: If email or username already exists, or validation fails
        """
        with self.session_factory() as session:
            with session.begin():
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

                logger.info(
                    f"Registered user: {email} (user_id={user_id}, username={username})"
                )

                # TODO: Send verification email
                # send_verification_email(user.email, user.user_id)

                return user

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
                "tenant_id": None,  # TODO: Get from ReBAC groups
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
            }

            token = self.create_token(user.email, user_info)
            logger.info(f"Login successful: {identifier} (user_id={user.user_id})")
            return token

    def change_password(
        self, user_id: str, old_password: str, new_password: str
    ) -> bool:
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
        with self.session_factory() as session:
            with session.begin():
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
                new_password_hash = bcrypt_lib.hashpw(new_password_bytes, salt).decode(
                    "utf-8"
                )

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
        with self.session_factory() as session:
            with session.begin():
                user = get_user_by_id(session, user_id)
                if not user:
                    return None

                if display_name is not None:
                    user.display_name = display_name

                if avatar_url is not None:
                    user.avatar_url = avatar_url

                if metadata is not None:
                    user.user_metadata = metadata

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
                "api_key": user.api_key if hasattr(user, 'api_key') else None,
                "tenant_id": user.tenant_id if hasattr(user, 'tenant_id') else None,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login_at": (
                    user.last_login_at.isoformat() if user.last_login_at else None
                ),
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
                "tenant_id": user.tenant_id,
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
