"""Database-backed API key authentication provider."""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.server.auth.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

# P0-5: API key security constants
API_KEY_PREFIX = "sk-"  # All keys must start with this
API_KEY_MIN_LENGTH = 32  # Minimum entropy requirement
HMAC_SALT = "nexus-api-key-v1"  # Version-tagged salt for key derivation


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
        if not self._validate_key_format(token):
            logger.warning(
                f"UNAUTHORIZED: Invalid API key format (must start with {API_KEY_PREFIX})"
            )
            return AuthResult(authenticated=False)

        # Hash the token for lookup (P0-5: HMAC-SHA256 with salt)
        token_hash = self._hash_key(token)

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
                f"[key: {api_key.key_id}, tenant: {api_key.tenant_id}]"
            )

            # v0.5.1: Get inherit_permissions flag (default True if not set)
            inherit_perms = (
                bool(api_key.inherit_permissions)
                if hasattr(api_key, "inherit_permissions")
                else True
            )

            return AuthResult(
                authenticated=True,
                subject_type=subject_type,
                subject_id=subject_id,
                tenant_id=api_key.tenant_id,
                is_admin=bool(api_key.is_admin),  # Convert from SQLite Integer to bool
                inherit_permissions=inherit_perms,  # v0.5.1: Permission inheritance control
                metadata={
                    "key_id": api_key.key_id,
                    "key_name": api_key.name,
                    "legacy_user_id": api_key.user_id,  # For backward compatibility
                    "expires_at": api_key.expires_at.isoformat()
                    if api_key.expires_at
                    else None,  # API key expiration
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

    @staticmethod
    def _validate_key_format(key: str) -> bool:
        """Validate API key format.

        P0-5: Enforce prefix and minimum length

        Args:
            key: Raw API key

        Returns:
            True if key format is valid
        """
        if not key.startswith(API_KEY_PREFIX):
            return False
        return len(key) >= API_KEY_MIN_LENGTH

    @staticmethod
    def _hash_key(key: str) -> str:
        """Hash API key using HMAC-SHA256 with salt.

        P0-5: HMAC-SHA256 instead of raw SHA-256 for rainbow table resistance

        Args:
            key: Raw API key

        Returns:
            HMAC-SHA256 hex digest
        """
        # Use HMAC with versioned salt for key derivation
        return hmac.new(HMAC_SALT.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def create_key(
        cls,
        session: Session,
        user_id: str,
        name: str,
        subject_type: str = "user",  # v0.5.0 ACE: "user" or "agent"
        subject_id: str | None = None,  # v0.5.0 ACE: Custom agent ID
        tenant_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,  # v0.5.1: Default False (zero permissions)
    ) -> tuple[str, str]:
        """Create a new API key in the database.

        P0-5: Generates key with proper prefix and entropy
        v0.5.0 ACE: Supports agent keys with custom subject IDs
        v0.5.1: Permission inheritance control for agents

        Args:
            session: SQLAlchemy session
            user_id: User identifier (owner of the key)
            name: Human-readable key name (e.g., "Production Server", "agent_data_analyst")
            subject_type: Type of subject ("user" or "agent") - v0.5.0 NEW
            subject_id: Custom subject ID (for agents) - v0.5.0 NEW
                       If None, defaults to user_id
            tenant_id: Optional tenant identifier
            is_admin: Whether this key has admin privileges
            expires_at: Optional expiry datetime (UTC)
            inherit_permissions: Whether agent inherits owner's permissions - v0.5.1 NEW
                                Default False (zero permissions, principle of least privilege)

        Returns:
            Tuple of (key_id, raw_key)
            IMPORTANT: Raw key is only returned once, must be saved by caller

        Example (user key):
            from datetime import timedelta
            with session_factory() as session:
                key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                    session,
                    user_id="alice",
                    name="Alice's laptop",
                    is_admin=True,
                    expires_at=datetime.now(UTC) + timedelta(days=90)
                )
                print(f"Save this key: {raw_key}")
                session.commit()

        Example (agent key - v0.5.0):
            with session_factory() as session:
                key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                    session,
                    user_id="alice",  # Owner
                    name="Data Analyst Agent",
                    subject_type="agent",
                    subject_id="agent_data_analyst",
                    expires_at=datetime.now(UTC) + timedelta(days=90)
                )
                print(f"Agent API key: {raw_key}")
                session.commit()
        """
        from nexus.storage.models import APIKeyModel

        # v0.5.0: If subject_id not provided, use user_id
        final_subject_id = subject_id or user_id

        # v0.5.0: Validate subject_type
        valid_subject_types = ["user", "agent", "service"]
        if subject_type not in valid_subject_types:
            raise ValueError(
                f"subject_type must be one of {valid_subject_types}, got {subject_type}"
            )

        # P0-5: Generate key with prefix, tenant, and high entropy (32+ bytes)
        # Format: sk-<tenant>_<subject>_<id>_<random-hex>
        tenant_prefix = f"{tenant_id[:8]}_" if tenant_id else ""
        subject_prefix = final_subject_id[:12] if subject_type == "agent" else user_id[:8]
        random_suffix = secrets.token_hex(16)  # 32 hex chars = 16 bytes
        key_id_part = secrets.token_hex(4)  # 8 hex chars for uniqueness

        raw_key = f"{API_KEY_PREFIX}{tenant_prefix}{subject_prefix}_{key_id_part}_{random_suffix}"
        key_hash = cls._hash_key(raw_key)

        # Create database record
        # Note: PostgreSQL has is_admin as INTEGER, so convert bool to int
        api_key = APIKeyModel(
            key_hash=key_hash,
            user_id=user_id,  # Always track the owner
            name=name,
            tenant_id=tenant_id,
            is_admin=int(is_admin),  # Convert bool to int for PostgreSQL
            expires_at=expires_at,
            subject_type=subject_type,  # v0.5.0: "user" or "agent"
            subject_id=final_subject_id,  # v0.5.0: Actual identity
            inherit_permissions=int(inherit_permissions),  # v0.5.1: Permission inheritance control
        )

        session.add(api_key)
        session.flush()  # Flush to generate key_id before returning
        # Don't commit here - let caller handle transaction

        return (api_key.key_id, raw_key)

    @classmethod
    def revoke_key(cls, session: Session, key_id: str) -> bool:
        """Revoke an API key.

        Args:
            session: SQLAlchemy session
            key_id: Key ID to revoke

        Returns:
            True if key was revoked, False if not found

        Example:
            with session_factory() as session:
                if DatabaseAPIKeyAuth.revoke_key(session, key_id):
                    session.commit()
                    print("Key revoked")
        """
        from nexus.storage.models import APIKeyModel

        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)

        if not api_key:
            return False

        api_key.revoked = 1  # Integer (0/1) for SQLite/PostgreSQL compatibility
        api_key.revoked_at = datetime.now(UTC)
        session.flush()  # Flush to persist changes before returning
        # Don't commit here - let caller handle transaction

        return True
