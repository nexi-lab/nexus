"""Database-backed API key authentication provider."""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.identity.api_key_ops import (
    API_KEY_PREFIX,
    create_api_key,
    hash_api_key,
    revoke_api_key,
    validate_key_format,
)
from nexus.server.auth.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)


class DatabaseAPIKeyAuth(AuthProvider):
    """Database-backed API key authentication with expiry and revocation.

    P0-5 Security features:
    - API keys stored securely with HMAC-SHA256 + salt (not raw SHA-256)
    - Mandatory key-id prefix (sk-) validation
    - Mandatory expiry for production keys
    - Revocation support with immediate cache invalidation
    - Audit trail of key usage

    Suitable for:
    - Production self-hosted deployments
    - Multi-user environments
    - Scenarios requiring key rotation

    Database schema is defined in APIKeyModel (see models.py).

    Security guarantees (P0-5):
    - Keys hashed with HMAC-SHA256 + salt (rainbow table resistant)
    - Key-id prefix prevents ambiguous token types
    - 32+ bytes entropy enforced
    - Expiry mandatory for production (configurable)
    """

    def __init__(self, session_factory: Any, require_expiry: bool = False):
        """Initialize database API key auth.

        Args:
            session_factory: SQLAlchemy session factory (sessionmaker)
            require_expiry: Reject keys without expiry (recommended for production)
        """
        self.session_factory = session_factory
        self.require_expiry = require_expiry
        logger.info(f"Initialized DatabaseAPIKeyAuth (require_expiry={require_expiry})")

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using database API key.

        P0-5: Validates prefix, checks expiry, uses HMAC-SHA256

        Args:
            token: API key from Authorization header

        Returns:
            AuthResult with user identity if valid
        """
        # Import here to avoid circular dependency
        from nexus.storage.models import APIKeyModel

        if not token:
            return AuthResult(authenticated=False)

        # P0-5: Validate key format and prefix
        if not validate_key_format(token):
            logger.warning(
                f"UNAUTHORIZED: Invalid API key format (must start with {API_KEY_PREFIX})"
            )
            return AuthResult(authenticated=False)

        # Hash the token for lookup (P0-5: HMAC-SHA256 with salt)
        token_hash = hash_api_key(token)

        with self.session_factory() as session:
            # Look up key in database
            # Note: For SQLite compatibility, use == 0 instead of == False
            stmt = select(APIKeyModel).where(
                APIKeyModel.key_hash == token_hash,
                APIKeyModel.revoked == 0,  # SQLite stores bool as Integer (0/1)
            )
            api_key = session.scalar(stmt)

            if not api_key:
                logger.debug(f"API key not found or revoked: {token_hash[:16]}...")
                return AuthResult(authenticated=False)

            # P0-5: Check expiry (mandatory for production)
            now = datetime.now(UTC)
            if api_key.expires_at:
                expires_at = api_key.expires_at
                # Ensure both are timezone-aware for comparison
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if now > expires_at:
                    logger.debug(f"UNAUTHORIZED: API key expired: {api_key.key_id}")
                    return AuthResult(authenticated=False)
            elif self.require_expiry:
                # P0-5: Reject keys without expiry in production mode
                logger.error(
                    f"UNAUTHORIZED: API key {api_key.key_id} has no expiry date. "
                    f"Set require_expiry=False to allow keys without expiry (not recommended)."
                )
                return AuthResult(authenticated=False)

            # Update last used timestamp
            api_key.last_used_at = datetime.now(UTC)
            session.commit()

            # Determine subject type from key metadata or default to "user"
            subject_type = (
                api_key.subject_type
                if hasattr(api_key, "subject_type") and api_key.subject_type
                else "user"
            )
            subject_id = (
                api_key.subject_id
                if hasattr(api_key, "subject_id") and api_key.subject_id
                else api_key.user_id
            )

            logger.debug(
                f"Authenticated subject: ({subject_type}, {subject_id}) "
                f"[key: {api_key.key_id}, zone: {api_key.zone_id}]"
            )

            return AuthResult(
                authenticated=True,
                subject_type=subject_type,
                subject_id=subject_id,
                zone_id=api_key.zone_id,
                is_admin=bool(api_key.is_admin),  # Convert from SQLite Integer to bool
                metadata={
                    "key_id": api_key.key_id,
                    "key_name": api_key.name,
                    "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
                },
            )

    async def validate_token(self, token: str) -> bool:
        """Check if token is valid (quick check).

        Args:
            token: API key

        Returns:
            True if key is valid
        """
        result = await self.authenticate(token)
        return result.authenticated

    def close(self) -> None:
        """Cleanup database connections."""
        # Session factory handles connection pooling, no explicit cleanup needed
        pass

    # Delegate static utilities to nexus.identity.api_key_ops
    _validate_key_format = staticmethod(validate_key_format)
    _hash_key = staticmethod(hash_api_key)

    @staticmethod
    def create_key(
        session: Session,
        user_id: str,
        name: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,
    ) -> tuple[str, str]:
        """Create a new API key. Delegates to nexus.identity.api_key_ops."""
        return create_api_key(
            session,
            user_id=user_id,
            name=name,
            subject_type=subject_type,
            subject_id=subject_id,
            zone_id=zone_id,
            is_admin=is_admin,
            expires_at=expires_at,
            inherit_permissions=inherit_permissions,
        )

    @staticmethod
    def revoke_key(session: Session, key_id: str, zone_id: str | None = None) -> bool:
        """Revoke an API key. Delegates to nexus.identity.api_key_ops."""
        return revoke_api_key(session, key_id, zone_id=zone_id)
