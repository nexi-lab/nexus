"""Centralized OAuth token management with automatic refresh.

Provides a unified interface for managing OAuth credentials across all providers.
Combines MindsDB's simple refresh pattern with centralized storage and audit logging.

Key features:
- Encrypted token storage in database
- Automatic token refresh on expiry
- Multi-provider support (Google, Microsoft, etc.)
- Tenant isolation
- Audit logging
- Token revocation

Example:
    >>> from nexus.server.auth.token_manager import TokenManager
    >>> from nexus.server.auth.google_oauth import GoogleOAuthProvider
    >>>
    >>> # Initialize manager
    >>> manager = TokenManager(db_path="nexus.db")
    >>>
    >>> # Register a provider
    >>> google_provider = GoogleOAuthProvider(
    ...     client_id="...",
    ...     client_secret="...",
    ...     scopes=["https://www.googleapis.com/auth/drive"]
    ... )
    >>> manager.register_provider("google", google_provider)
    >>>
    >>> # Store credentials (after OAuth flow)
    >>> await manager.store_credential(
    ...     provider="google",
    ...     user_email="alice@example.com",
    ...     credential=credential
    ... )
    >>>
    >>> # Get valid token (automatic refresh if expired)
    >>> token = await manager.get_valid_token("google", "alice@example.com")
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.core.exceptions import AuthenticationError
from nexus.storage.models import Base, OAuthCredentialModel

from .oauth_crypto import OAuthCrypto
from .oauth_provider import OAuthCredential, OAuthError, OAuthProvider

logger = logging.getLogger(__name__)


class TokenManager:
    """Centralized OAuth token manager with automatic refresh.

    Manages OAuth credentials for all providers (Google, Microsoft, etc.).
    Provides automatic token refresh following MindsDB's pattern.

    Security features:
    - Encrypted token storage (Fernet)
    - Tenant isolation
    - Audit logging
    - Automatic expiry enforcement

    Example:
        >>> manager = TokenManager(db_path="nexus.db")
        >>> manager.register_provider("google", GoogleOAuthProvider(...))
        >>> token = await manager.get_valid_token("google", "alice@example.com")
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        db_url: str | None = None,
        encryption_key: str | None = None,
    ):
        """Initialize token manager.

        Args:
            db_path: Path to SQLite database (deprecated, use db_url)
            db_url: Database URL (e.g., 'sqlite:///path/to/db' or 'postgresql://user:pass@host/db')
            encryption_key: Fernet encryption key (base64-encoded)

        Example:
            >>> manager = TokenManager(db_path="nexus.db")
            >>> # or
            >>> manager = TokenManager(db_url="postgresql://user:pass@host/db")
        """
        # Setup database
        if db_url:
            self.database_url = db_url
        elif db_path:
            self.database_url = f"sqlite:///{db_path}"
        else:
            raise ValueError("Either db_path or db_url must be provided")

        # Create engine and session factory
        self.engine = create_engine(
            self.database_url,
            connect_args={"check_same_thread": False} if "sqlite" in self.database_url else {},
        )
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Create tables if they don't exist
        Base.metadata.create_all(self.engine)

        # Setup encryption - pass db_url so key can be stored/retrieved from database
        self.crypto = OAuthCrypto(encryption_key=encryption_key, db_url=self.database_url)

        # Provider registry
        self.providers: dict[str, OAuthProvider] = {}

    def register_provider(self, provider_name: str, provider: OAuthProvider) -> None:
        """Register an OAuth provider.

        Args:
            provider_name: Provider name (e.g., "google", "microsoft")
            provider: OAuthProvider instance

        Example:
            >>> manager.register_provider("google", GoogleOAuthProvider(...))
        """
        self.providers[provider_name] = provider
        logger.info(f"Registered OAuth provider: {provider_name}")

    async def store_credential(
        self,
        provider: str,
        user_email: str,
        credential: OAuthCredential,
        tenant_id: str = "default",
        created_by: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Store OAuth credential in database.

        Args:
            provider: Provider name (e.g., "google")
            user_email: User's email address (from OAuth provider)
            credential: OAuthCredential to store
            tenant_id: Tenant ID (defaults to "default")
            created_by: Optional creator user ID
            user_id: Optional Nexus user ID (for permission checks, preferred over created_by)

        Returns:
            credential_id: Database credential ID

        Raises:
            ValueError: If provider is invalid or credential is invalid

        Example:
            >>> cred_id = await manager.store_credential(
            ...     provider="google",
            ...     user_email="alice@example.com",
            ...     credential=credential,
            ...     tenant_id="org_acme",
            ...     user_id="alice"
            ... )
        """
        if not provider or not provider.strip():
            raise ValueError("Provider name cannot be empty")

        # Encrypt tokens
        encrypted_access_token = self.crypto.encrypt_token(credential.access_token)
        encrypted_refresh_token = None
        if credential.refresh_token:
            encrypted_refresh_token = self.crypto.encrypt_token(credential.refresh_token)

        # Serialize scopes to JSON
        scopes_json = None
        if credential.scopes:
            scopes_json = json.dumps(credential.scopes)

        with self.SessionLocal() as session:
            # Check if credential already exists (upsert)
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.tenant_id == tenant_id,
            )
            existing = session.execute(stmt).scalar_one_or_none()

            # Use provided user_id, or fall back to created_by if it's not an email
            # user_id parameter takes precedence (from context.user_id)
            if user_id is None and created_by and created_by != user_email:
                user_id = created_by

            if existing:
                # Update existing credential
                existing.encrypted_access_token = encrypted_access_token
                existing.encrypted_refresh_token = encrypted_refresh_token
                existing.token_type = credential.token_type
                existing.expires_at = credential.expires_at
                existing.scopes = scopes_json
                existing.client_id = credential.client_id
                existing.token_uri = credential.token_uri
                existing.user_id = user_id  # Update user_id if provided
                existing.updated_at = datetime.now(UTC)
                existing.revoked = 0  # Un-revoke if updating
                session.commit()

                logger.info(
                    f"Updated OAuth credential: {provider}:{user_email} (user_id={user_id})"
                )
                self._log_audit("credential_updated", provider, user_email, tenant_id)
                return existing.credential_id
            else:
                # Create new credential
                model = OAuthCredentialModel(
                    provider=provider,
                    user_email=user_email,
                    user_id=user_id,  # Link to Nexus user_id for permission checks
                    tenant_id=tenant_id,
                    encrypted_access_token=encrypted_access_token,
                    encrypted_refresh_token=encrypted_refresh_token,
                    token_type=credential.token_type,
                    expires_at=credential.expires_at,
                    scopes=scopes_json,
                    client_id=credential.client_id,
                    token_uri=credential.token_uri,
                    created_by=created_by,
                )

                session.add(model)
                session.commit()
                session.refresh(model)

                logger.info(f"Stored OAuth credential: {provider}:{user_email}")
                self._log_audit("credential_created", provider, user_email, tenant_id)
                return model.credential_id

    async def get_valid_token(
        self, provider: str, user_email: str, tenant_id: str = "default"
    ) -> str:
        """Get a valid access token (with automatic refresh if expired).

        This is the core method following MindsDB's refresh pattern:
        1. Retrieve credential from database
        2. Decrypt tokens
        3. Check if expired
        4. If expired and refresh_token exists, refresh
        5. Update database with new tokens
        6. Return valid access_token

        Args:
            provider: Provider name (e.g., "google")
            user_email: User's email address
            tenant_id: Optional tenant ID (defaults to 'default')

        Returns:
            Valid access token (decrypted)

        Raises:
            AuthenticationError: If credential not found or refresh fails

        Example (MindsDB pattern):
            >>> token = await manager.get_valid_token("google", "alice@example.com")
            >>> # Token is guaranteed to be valid (refreshed if needed)
        """
        # Default tenant_id to 'default' if not provided
        if tenant_id is None:
            tenant_id = "default"

        with self.SessionLocal() as session:
            # Retrieve credential from database
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.tenant_id == tenant_id,
                OAuthCredentialModel.revoked == 0,
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                raise AuthenticationError(f"No OAuth credential found for {provider}:{user_email}")

            # Decrypt credential
            credential = self._model_to_credential(model)

            # MindsDB pattern: check if expired and refresh if needed
            if credential.is_expired() and credential.refresh_token:
                logger.info(f"Token expired for {provider}:{user_email}, refreshing...")

                # Get provider and refresh token
                if provider not in self.providers:
                    raise AuthenticationError(f"Provider not registered: {provider}")

                oauth_provider = self.providers[provider]

                try:
                    # Refresh token
                    new_credential = await oauth_provider.refresh_token(credential)

                    # Update database with new tokens
                    encrypted_access_token = self.crypto.encrypt_token(new_credential.access_token)
                    encrypted_refresh_token = None
                    if new_credential.refresh_token:
                        encrypted_refresh_token = self.crypto.encrypt_token(
                            new_credential.refresh_token
                        )

                    model.encrypted_access_token = encrypted_access_token
                    if encrypted_refresh_token:
                        model.encrypted_refresh_token = encrypted_refresh_token
                    model.expires_at = new_credential.expires_at
                    model.last_refreshed_at = datetime.now(UTC)
                    model.updated_at = datetime.now(UTC)

                    session.commit()

                    logger.info(f"Token refreshed successfully for {provider}:{user_email}")
                    self._log_audit("token_refreshed", provider, user_email, tenant_id)

                    # Use new credential
                    credential = new_credential

                except OAuthError as e:
                    logger.error(f"Failed to refresh token for {provider}:{user_email}: {e}")
                    raise AuthenticationError(f"Failed to refresh token: {e}") from e

            # Update last_used_at
            model.last_used_at = datetime.now(UTC)
            session.commit()

            return credential.access_token

    async def get_credential(
        self, provider: str, user_email: str, tenant_id: str = "default"
    ) -> OAuthCredential | None:
        """Get credential (decrypted) without automatic refresh.

        Args:
            provider: Provider name
            user_email: User's email
            tenant_id: Tenant ID (defaults to "default")

        Returns:
            OAuthCredential or None if not found
        """
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.tenant_id == tenant_id,
                OAuthCredentialModel.revoked == 0,
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                return None

            return self._model_to_credential(model)

    async def revoke_credential(
        self, provider: str, user_email: str, tenant_id: str = "default"
    ) -> bool:
        """Revoke an OAuth credential.

        Args:
            provider: Provider name
            user_email: User's email
            tenant_id: Tenant ID (defaults to "default")

        Returns:
            True if revoked successfully

        Example:
            >>> await manager.revoke_credential("google", "alice@example.com")
        """
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.tenant_id == tenant_id,
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                return False

            # Revoke via provider API (if available)
            if provider in self.providers:
                credential = self._model_to_credential(model)
                oauth_provider = self.providers[provider]
                try:
                    await oauth_provider.revoke_token(credential)
                except Exception as e:
                    logger.warning(f"Failed to revoke via provider API: {e}")

            # Mark as revoked in database
            model.revoked = 1
            model.revoked_at = datetime.now(UTC)
            session.commit()

            logger.info(f"Revoked OAuth credential: {provider}:{user_email}")
            self._log_audit("credential_revoked", provider, user_email, tenant_id)
            return True

    async def list_credentials(
        self,
        tenant_id: str | None = None,
        user_email: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all credentials (metadata only, no tokens).

        Args:
            tenant_id: Optional tenant ID to filter by
            user_email: Optional user email to filter by (OAuth provider email)
            user_id: Optional user ID to filter by (Nexus user identity, preferred)

        Returns:
            List of credential metadata dicts

        Example:
            >>> # List all credentials for a tenant
            >>> credentials = await manager.list_credentials(tenant_id="org_acme")
            >>> # List credentials for a specific user (by user_id, preferred)
            >>> credentials = await manager.list_credentials(
            ...     tenant_id="org_acme",
            ...     user_id="alice"
            ... )
            >>> # List credentials for a specific user (by email, fallback)
            >>> credentials = await manager.list_credentials(
            ...     tenant_id="org_acme",
            ...     user_email="alice@example.com"
            ... )
        """
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(OAuthCredentialModel.revoked == 0)

            if tenant_id is not None:
                stmt = stmt.where(OAuthCredentialModel.tenant_id == tenant_id)

            # Prefer user_id over user_email for filtering (more reliable)
            if user_id is not None:
                stmt = stmt.where(OAuthCredentialModel.user_id == user_id)
            elif user_email is not None:
                stmt = stmt.where(OAuthCredentialModel.user_email == user_email)

            models = session.execute(stmt).scalars().all()

            return [
                {
                    "credential_id": model.credential_id,
                    "provider": model.provider,
                    "user_email": model.user_email,
                    "user_id": model.user_id,  # Nexus user identity (may differ from email)
                    "tenant_id": model.tenant_id,
                    "expires_at": model.expires_at.isoformat() if model.expires_at else None,
                    "is_expired": model.is_expired(),
                    "created_at": model.created_at.isoformat(),
                    "last_used_at": (
                        model.last_used_at.isoformat() if model.last_used_at else None
                    ),
                }
                for model in models
            ]

    def _model_to_credential(self, model: OAuthCredentialModel) -> OAuthCredential:
        """Convert database model to OAuthCredential (decrypted).

        Args:
            model: OAuthCredentialModel from database

        Returns:
            OAuthCredential with decrypted tokens
        """
        # Decrypt tokens
        access_token = self.crypto.decrypt_token(model.encrypted_access_token)
        refresh_token = None
        if model.encrypted_refresh_token:
            refresh_token = self.crypto.decrypt_token(model.encrypted_refresh_token)

        # Parse scopes
        scopes = None
        if model.scopes:
            scopes = json.loads(model.scopes)

        # Ensure expires_at is timezone-aware (convert if naive)
        expires_at = model.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        cred = OAuthCredential(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=model.token_type,
            expires_at=expires_at,
            scopes=scopes,
            provider=model.provider,
            user_email=model.user_email,
            client_id=model.client_id,
            token_uri=model.token_uri,
        )
        # Store user_id in metadata for permission checks
        if model.user_id:
            if cred.metadata is None:
                cred.metadata = {}
            cred.metadata["user_id"] = model.user_id
        return cred

    def _log_audit(
        self, operation: str, provider: str, user_email: str, tenant_id: str | None
    ) -> None:
        """Log audit trail for token operations.

        Args:
            operation: Operation type (e.g., "token_refreshed")
            provider: Provider name
            user_email: User's email
            tenant_id: Optional tenant ID
        """
        # TODO: Implement proper audit logging to database
        # For now, just log to application logger
        logger.info(
            f"AUDIT: {operation} | provider={provider} | user={user_email} | tenant={tenant_id}"
        )

    def close(self) -> None:
        """Cleanup resources with Windows file locking support."""
        import gc
        import sys
        import time

        if not hasattr(self, "engine"):
            return  # Already closed or never initialized

        # Prevent double-close
        if getattr(self, "_closed", False):
            return
        self._closed = True

        try:
            # CRITICAL: Force garbage collection BEFORE closing database
            # This ensures any lingering session references are cleaned up first
            # Especially important on Windows where sessions may hold file locks
            gc.collect()
            gc.collect(1)
            gc.collect(2)
            # Brief wait to let OS release file handles
            if sys.platform == "win32":
                time.sleep(0.05)  # 50ms on Windows
            else:
                time.sleep(0.01)  # 10ms elsewhere

            # SQLite-specific cleanup
            if "sqlite" in self.database_url:
                # For SQLite, checkpoint WAL/journal files before disposing
                try:
                    from sqlalchemy import text

                    # Create a new connection to ensure we have exclusive access
                    with self.engine.connect() as conn:
                        # Checkpoint WAL file to merge changes back to main database
                        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                        conn.commit()

                        # Switch to DELETE mode to remove WAL files
                        conn.execute(text("PRAGMA journal_mode=DELETE"))
                        conn.commit()

                        # Close the connection explicitly
                        conn.close()
                except Exception:
                    # Ignore errors during checkpoint (e.g., database already closed or locked)
                    pass

            # Dispose of the connection pool - this closes all connections
            # Note: All sessions should be closed via context managers (with statements)
            # before this point. The dispose() call will close any remaining connections.
            self.engine.dispose()

            # CRITICAL: On Windows, force GC after disposal to release lingering references
            gc.collect()
            gc.collect(1)
            gc.collect(2)
            # Minimal wait for OS to release handles
            if sys.platform == "win32":
                time.sleep(0.1)  # 100ms on Windows
            else:
                time.sleep(0.01)  # 10ms elsewhere

            # SQLite-specific file cleanup
            if "sqlite" in self.database_url and hasattr(self, "database_url"):
                # Extract db_path from sqlite:///path/to/db
                db_path_str = self.database_url.replace("sqlite:///", "")
                if db_path_str:
                    try:
                        from pathlib import Path

                        db_path = Path(db_path_str)
                        # Additional cleanup: Try to remove any lingering SQLite temp files
                        # This helps with test cleanup when using tempfile.TemporaryDirectory()
                        for suffix in ["-wal", "-shm", "-journal"]:
                            temp_file = db_path.parent / f"{db_path.name}{suffix}"
                            if temp_file.exists():
                                import contextlib

                                # Ignore errors - file may be locked or already deleted
                                with contextlib.suppress(OSError, PermissionError):
                                    temp_file.unlink(missing_ok=True)
                    except Exception:
                        # Ignore errors during temp file cleanup
                        pass
        except Exception:
            # Ensure engine is disposed even if cleanup fails
            import contextlib

            with contextlib.suppress(Exception):
                self.engine.dispose()
