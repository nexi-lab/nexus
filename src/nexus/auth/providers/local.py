"""Local username/password authentication with JWT tokens."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import bcrypt as bcrypt_lib
from authlib.jose import JoseError, jwt

from nexus.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)


class LocalAuth(AuthProvider):
    """Local username/password authentication with JWT tokens.

    Supports:
    - Username/password authentication with bcrypt hashing
    - JWT token generation and validation (HS256)
    - In-memory user management
    - Subject-based identity (user, agent, service, session)

    Security:
    - Passwords hashed with bcrypt (12 rounds)
    - JWT tokens signed with HS256
    - Tokens expire after 1 hour (configurable)
    """

    def __init__(
        self,
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
        users: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.jwt_secret = jwt_secret or secrets.token_urlsafe(32)
        self.token_expiry = token_expiry
        self.users = users or {}

        if not jwt_secret:
            logger.warning(
                "No JWT secret provided - auto-generated. "
                "Tokens will be invalidated on restart."
            )
        logger.info("Initialized LocalAuth with %d users", len(self.users))

    def create_user(
        self,
        email: str,
        password: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new user account.

        Returns:
            User info dictionary (without password hash).

        Raises:
            ValueError: If user already exists.
        """
        if email in self.users:
            raise ValueError(f"User {email} already exists")

        password_bytes = password.encode("utf-8")
        salt = bcrypt_lib.gensalt()
        password_hash = bcrypt_lib.hashpw(password_bytes, salt).decode("utf-8")

        user_info = {
            "password_hash": password_hash,
            "subject_type": subject_type,
            "subject_id": subject_id or email.split("@")[0],
            "zone_id": zone_id,
            "is_admin": is_admin,
            "name": name or email.split("@")[0],
            "metadata": metadata or {},
        }

        self.users[email] = user_info
        logger.info(
            "Created user: %s (subject: %s:%s)", email, subject_type, user_info["subject_id"]
        )

        return {k: v for k, v in user_info.items() if k != "password_hash"}

    def verify_password(self, email: str, password: str) -> dict[str, Any] | None:
        """Verify email/password credentials.

        Returns:
            User info dict if valid, None otherwise.
        """
        user = self.users.get(email)
        if not user:
            return None

        password_bytes = password.encode("utf-8")
        stored_hash = user["password_hash"].encode("utf-8")
        if not bcrypt_lib.checkpw(password_bytes, stored_hash):
            return None

        return user

    def create_token(self, email: str, user_info: dict[str, Any]) -> str:
        """Create JWT token for user."""
        header = {"alg": "HS256"}
        payload = {
            "sub": user_info["subject_id"],
            "email": email,
            "subject_type": user_info["subject_type"],
            "subject_id": user_info["subject_id"],
            "zone_id": user_info.get("zone_id"),
            "is_admin": user_info.get("is_admin", False),
            "name": user_info.get("name", email),
            "iat": int(time.time()),
            "exp": int(time.time()) + self.token_expiry,
        }
        agent_generation = user_info.get("agent_generation")
        if agent_generation is not None:
            payload["agent_generation"] = agent_generation

        token = jwt.encode(header, payload, self.jwt_secret)
        result: str = token.decode() if isinstance(token, bytes) else token
        return result

    def verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode JWT token.

        Raises:
            ValueError: If token is invalid or expired.
        """
        try:
            claims = jwt.decode(token, self.jwt_secret)
            claims.validate()
            result: dict[str, Any] = dict(claims)
            return result
        except JoseError as e:
            raise ValueError(f"Invalid token: {e}") from e

    def verify_password_and_create_token(self, email: str, password: str) -> str | None:
        """Verify password and create JWT token in one step."""
        user_info = self.verify_password(email, password)
        if not user_info:
            return None
        return self.create_token(email, user_info)

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using JWT token."""
        try:
            claims = self.verify_token(token)

            raw_gen = claims.get("agent_generation")
            try:
                agent_generation = int(raw_gen) if raw_gen is not None else None
            except (ValueError, TypeError):
                logger.warning("Invalid agent_generation in JWT: %r, treating as None", raw_gen)
                agent_generation = None

            return AuthResult(
                authenticated=True,
                subject_type=claims.get("subject_type", "user"),
                subject_id=claims.get("subject_id"),
                zone_id=claims.get("zone_id"),
                is_admin=claims.get("is_admin", False),
                metadata={"email": claims.get("email"), "name": claims.get("name")},
                agent_generation=agent_generation,
            )
        except ValueError as e:
            logger.debug("Authentication failed: %s", e)
            return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        try:
            self.verify_token(token)
            return True
        except ValueError:
            return False

    def close(self) -> None:
        pass

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LocalAuth:
        """Create from configuration dictionary."""
        return cls(
            jwt_secret=config.get("jwt_secret"),
            token_expiry=config.get("token_expiry", 3600),
            users=config.get("users", {}),
        )
