"""Local username/password authentication with JWT tokens."""

import logging
import secrets
import time
from typing import Any

import bcrypt as bcrypt_lib
from authlib.jose import JoseError, jwt

from nexus.server.auth.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)


class LocalAuth(AuthProvider):
    """Local username/password authentication with JWT tokens.

    This provider supports:
    - Username/password authentication with bcrypt hashing
    - JWT token generation and validation
    - User management (create, verify)
    - Subject-based identity (user, agent, service, session)

    Example usage:
        # Create auth provider
        auth = LocalAuth(jwt_secret="your-secret-key")

        # Create user
        auth.create_user(
            email="alice@example.com",
            password="secure-password",
            subject_type="user",
            subject_id="alice",
            tenant_id="org_acme"
        )

        # Verify password and get token
        token = auth.verify_password_and_create_token("alice@example.com", "secure-password")

        # Authenticate with token
        result = await auth.authenticate(token)
        # → AuthResult(subject_type="user", subject_id="alice", ...)

    Security:
    - Passwords hashed with bcrypt (12 rounds)
    - JWT tokens signed with HS256
    - Tokens expire after 1 hour (configurable)
    - Auto-generates secure secrets if not provided
    """

    def __init__(
        self,
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
        users: dict[str, dict[str, Any]] | None = None,
    ):
        """Initialize local authentication.

        Args:
            jwt_secret: Secret key for JWT signing. Auto-generated if not provided.
            token_expiry: Token expiration in seconds (default: 3600 = 1 hour)
            users: Optional dict of users for in-memory storage:
                {
                    "alice@example.com": {
                        "password_hash": "bcrypt-hash",
                        "subject_type": "user",
                        "subject_id": "alice",
                        "tenant_id": "org_acme",
                        "is_admin": False,
                        "metadata": {}
                    }
                }
        """
        self.jwt_secret = jwt_secret or secrets.token_urlsafe(32)
        self.token_expiry = token_expiry
        self.users = users or {}

        if not jwt_secret:
            logger.warning(
                "No JWT secret provided - auto-generated. "
                "This will invalidate tokens on restart. "
                "Set NEXUS_JWT_SECRET environment variable for production."
            )

        logger.info(f"Initialized LocalAuth with {len(self.users)} users")

    def create_user(
        self,
        email: str,
        password: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new user account.

        Args:
            email: User email (used as login username)
            password: Plain-text password (will be hashed)
            subject_type: Type of subject ("user", "agent", "service", "session")
            subject_id: Unique identifier (defaults to email prefix)
            tenant_id: Optional tenant/organization ID
            is_admin: Whether user has admin privileges
            name: Display name (defaults to email prefix)
            metadata: Optional additional metadata

        Returns:
            User info dictionary (without password hash)

        Raises:
            ValueError: If user already exists
        """
        if email in self.users:
            raise ValueError(f"User {email} already exists")

        # Hash password (use bcrypt directly)
        password_bytes = password.encode("utf-8")
        salt = bcrypt_lib.gensalt()
        password_hash = bcrypt_lib.hashpw(password_bytes, salt).decode("utf-8")

        # Create user record
        user_info = {
            "password_hash": password_hash,
            "subject_type": subject_type,
            "subject_id": subject_id or email.split("@")[0],
            "tenant_id": tenant_id,
            "is_admin": is_admin,
            "name": name or email.split("@")[0],
            "metadata": metadata or {},
        }

        self.users[email] = user_info
        logger.info(f"Created user: {email} (subject: {subject_type}:{user_info['subject_id']})")

        # Return user info without password hash
        return {k: v for k, v in user_info.items() if k != "password_hash"}

    def verify_password(self, email: str, password: str) -> dict[str, Any] | None:
        """Verify email/password credentials.

        Args:
            email: User email
            password: Plain-text password

        Returns:
            User info dict if valid, None otherwise
        """
        user = self.users.get(email)
        if not user:
            return None

        # Verify password (use bcrypt directly)
        password_bytes = password.encode("utf-8")
        stored_hash = user["password_hash"].encode("utf-8")
        if not bcrypt_lib.checkpw(password_bytes, stored_hash):
            return None

        return user

    def create_token(self, email: str, user_info: dict[str, Any]) -> str:
        """Create JWT token for user.

        Args:
            email: User email
            user_info: User information dict

        Returns:
            Encoded JWT token string
        """
        header = {"alg": "HS256"}
        payload = {
            "sub": user_info["subject_id"],
            "email": email,
            "subject_type": user_info["subject_type"],
            "subject_id": user_info["subject_id"],
            "tenant_id": user_info.get("tenant_id"),
            "is_admin": user_info.get("is_admin", False),
            "name": user_info.get("name", email),
            "iat": int(time.time()),
            "exp": int(time.time()) + self.token_expiry,
        }

        token = jwt.encode(header, payload, self.jwt_secret)
        result: str = token.decode() if isinstance(token, bytes) else token
        return result

    def verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode JWT token.

        Args:
            token: JWT token string

        Returns:
            Decoded claims dict

        Raises:
            ValueError: If token is invalid or expired
        """
        try:
            claims = jwt.decode(token, self.jwt_secret)
            claims.validate()  # Validates expiry automatically
            result: dict[str, Any] = dict(claims)
            return result
        except JoseError as e:
            raise ValueError(f"Invalid token: {e}") from e

    def verify_password_and_create_token(self, email: str, password: str) -> str | None:
        """Verify password and create JWT token in one step.

        Args:
            email: User email
            password: Plain-text password

        Returns:
            JWT token if credentials valid, None otherwise
        """
        user_info = self.verify_password(email, password)
        if not user_info:
            return None

        return self.create_token(email, user_info)

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using JWT token.

        Args:
            token: JWT token from Authorization header

        Returns:
            AuthResult with subject identity if valid
        """
        try:
            claims = self.verify_token(token)

            return AuthResult(
                authenticated=True,
                subject_type=claims.get("subject_type", "user"),
                subject_id=claims.get("subject_id"),
                tenant_id=claims.get("tenant_id"),
                is_admin=claims.get("is_admin", False),
                metadata={"email": claims.get("email"), "name": claims.get("name")},
            )
        except ValueError as e:
            logger.debug(f"Authentication failed: {e}")
            return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        """Quick validation check without full authentication.

        Args:
            token: JWT token

        Returns:
            True if token is valid
        """
        try:
            self.verify_token(token)
            return True
        except ValueError:
            return False

    def close(self) -> None:
        """Cleanup resources (no-op for local auth)."""
        pass

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LocalAuth":
        """Create from configuration dictionary.

        Args:
            config: Configuration with optional fields:
                - jwt_secret: Secret key for JWT signing
                - token_expiry: Token expiration in seconds
                - users: Dict of user records

        Returns:
            LocalAuth instance

        Example:
            config = {
                "jwt_secret": "your-secret-key",
                "token_expiry": 3600,
                "users": {
                    "alice@example.com": {
                        "password_hash": "bcrypt-hash",
                        "subject_type": "user",
                        "subject_id": "alice"
                    }
                }
            }
            auth = LocalAuth.from_config(config)
        """
        return cls(
            jwt_secret=config.get("jwt_secret"),
            token_expiry=config.get("token_expiry", 3600),
            users=config.get("users", {}),
        )
