"""Database-backed username/password authentication with JWT tokens.

Extends LocalAuth to store users in the database (UserModel).
Supports registration, login, password management, and profile updates.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import bcrypt as bcrypt_lib
from sqlalchemy.orm import Session, sessionmaker

from nexus.bricks.auth.providers.local import LocalAuth
from nexus.bricks.auth.user_queries import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    validate_user_uniqueness,
)
from nexus.storage.models import UserModel

logger = logging.getLogger(__name__)


def _verify_user_login_eligibility(user: UserModel) -> None:
    """Check if user is eligible to login.

    Raises:
        ValueError: If user email is not verified.
    """
    if not user.email_verified:
        raise ValueError("Email not verified. Please check your inbox for verification link.")


class DatabaseLocalAuth(LocalAuth):
    """Database-backed local username/password authentication.

    Extends LocalAuth with persistent storage via SQLAlchemy UserModel.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
    ) -> None:
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

        Returns:
            Created UserModel.

        Raises:
            ValueError: If email or username already exists.
        """
        with self.session_factory() as session, session.begin():
            validate_user_uniqueness(session, email=email, username=username)

            password_bytes = password.encode("utf-8")
            salt = bcrypt_lib.gensalt(rounds=12)
            password_hash = bcrypt_lib.hashpw(password_bytes, salt).decode("utf-8")

            user_id = str(uuid.uuid4())

            user = UserModel(
                user_id=user_id,
                email=email,
                username=username,
                display_name=display_name or username or email.split("@")[0],
                password_hash=password_hash,
                primary_auth_method="password",
                is_global_admin=1 if is_admin else 0,
                is_active=1,
                email_verified=0,
                user_metadata=metadata,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )

            session.add(user)
            session.flush()
            session.expunge(user)

            logger.info("Registered user: %s (user_id=%s)", email, user_id)
            return user

    async def register(
        self,
        email: str,
        password: str,
        username: str | None = None,
        display_name: str | None = None,
    ) -> tuple[UserModel, str]:
        """Register a new user and return user with JWT token."""
        user = self.register_user(
            email=email,
            password=password,
            username=username,
            display_name=display_name,
        )

        user_info = {
            "subject_type": "user",
            "subject_id": user.user_id,
            "zone_id": None,
            "is_admin": user.is_global_admin == 1,
            "name": user.display_name or user.username or user.email,
        }

        assert user.email is not None
        token = self.create_token(user.email, user_info)
        return (user, token)

    def login(self, identifier: str, password: str) -> str | None:
        """Authenticate user and create JWT token."""
        with self.session_factory() as session:
            user = get_user_by_email(session, identifier)
            if not user:
                user = get_user_by_username(session, identifier)

            if not user:
                logger.debug("Login failed: user not found (%s)", identifier)
                return None

            if not user.password_hash:
                logger.debug("Login failed: no password set (%s)", identifier)
                return None

            password_bytes = password.encode("utf-8")
            stored_hash = user.password_hash.encode("utf-8")
            if not bcrypt_lib.checkpw(password_bytes, stored_hash):
                logger.debug("Login failed: invalid password (%s)", identifier)
                return None

            _verify_user_login_eligibility(user)

            user.last_login_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(user)
            session.commit()

            user_info = {
                "subject_type": "user",
                "subject_id": user.user_id,
                "zone_id": None,
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
            }

            assert user.email is not None
            token = self.create_token(user.email, user_info)
            logger.info("Login successful: %s (user_id=%s)", identifier, user.user_id)
            return token

    async def login_async(self, identifier: str, password: str) -> tuple[UserModel, str] | None:
        """Authenticate user and return user with JWT token.

        Raises:
            ValueError: If credentials are invalid.
        """
        token = self.login(identifier, password)
        if not token:
            raise ValueError("Invalid email/username or password")

        with self.session_factory() as session:
            user = get_user_by_email(session, identifier)
            if not user:
                user = get_user_by_username(session, identifier)
            if not user:
                raise ValueError("User not found after successful login")

            session.expunge(user)
            return (user, token)

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """Change user password.

        Raises:
            ValueError: If old password is incorrect or user not found.
        """
        with self.session_factory() as session, session.begin():
            user = get_user_by_id(session, user_id)
            if not user:
                raise ValueError(f"User not found: {user_id}")

            if not user.password_hash:
                raise ValueError("No password set for this user")

            password_bytes = old_password.encode("utf-8")
            stored_hash = user.password_hash.encode("utf-8")
            if not bcrypt_lib.checkpw(password_bytes, stored_hash):
                raise ValueError("Incorrect current password")

            new_password_bytes = new_password.encode("utf-8")
            salt = bcrypt_lib.gensalt(rounds=12)
            new_password_hash = bcrypt_lib.hashpw(new_password_bytes, salt).decode("utf-8")

            user.password_hash = new_password_hash
            user.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(user)

            logger.info("Password changed for user: %s", user_id)
            return True

    def update_profile(
        self,
        user_id: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserModel | None:
        """Update user profile."""
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

            user.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(user)
            session.flush()
            session.expunge(user)

            logger.info("Profile updated for user: %s", user_id)
            return user

    def get_user_info(self, user_id: str) -> dict[str, Any] | None:
        """Get user information by user_id."""
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
        """Get user information formatted for JWT token creation."""
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

    def create_email_verification_token(self, user_id: str, email: str) -> str:
        """Create a JWT token for email verification (24h expiry)."""
        import time

        from authlib.jose import jwt as jose_jwt

        header = {"alg": "HS256"}
        payload = {
            "sub": user_id,
            "email": email,
            "purpose": "email_verify",
            "iat": int(time.time()),
            "exp": int(time.time()) + 86400,
        }
        token = jose_jwt.encode(header, payload, self.jwt_secret)
        result: str = token.decode() if isinstance(token, bytes) else token
        return result

    def verify_email_token(self, token: str) -> tuple[str, str]:
        """Verify email verification token.

        Returns:
            Tuple of (user_id, email).

        Raises:
            ValueError: If token is invalid, expired, or wrong purpose.
        """
        from authlib.jose import JoseError
        from authlib.jose import jwt as jose_jwt

        try:
            claims = jose_jwt.decode(token, self.jwt_secret)
            claims.validate()
        except JoseError as e:
            raise ValueError(f"Invalid verification token: {e}") from e

        if claims.get("purpose") != "email_verify":
            raise ValueError("Invalid token: not an email verification token")

        user_id = claims.get("sub")
        email = claims.get("email")
        if not user_id or not email:
            raise ValueError("Invalid token: missing user information")

        return (str(user_id), str(email))

    def verify_email(self, user_id: str, verification_token: str) -> bool:
        """Verify user email.

        Raises:
            ValueError: If token is invalid or user not found.
        """
        token_user_id, _email = self.verify_email_token(verification_token)

        if token_user_id != user_id:
            raise ValueError("Token user_id does not match")

        with self.session_factory() as session, session.begin():
            user = get_user_by_id(session, user_id)
            if not user:
                raise ValueError(f"User not found: {user_id}")

            user.email_verified = 1
            user.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(user)

        logger.info("Email verified for user: %s", user_id)
        return True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DatabaseLocalAuth":
        """Create from configuration dictionary."""
        return cls(
            session_factory=config["session_factory"],
            jwt_secret=config.get("jwt_secret"),
            token_expiry=config.get("token_expiry", 3600),
        )
