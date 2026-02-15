"""Centralized OAuth token management with automatic refresh and rotation.

Provides a unified interface for managing OAuth credentials across all providers.
Combines MindsDB's simple refresh pattern with centralized storage, audit logging,
and RFC 9700 refresh token rotation with reuse detection.

Key features:
- Encrypted token storage in database
- Automatic token refresh on expiry
- Refresh token rotation with reuse detection (RFC 9700)
- Token family tracking and invalidation
- Multi-provider support (Google, Microsoft, etc.)
- Zone isolation
- Immutable secrets audit logging
- In-memory token caching (30s TTL)
- Per-credential refresh rate limiting (30s cooldown)

Issue #997: OAuth token rotation and secrets audit logging.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cachetools import TTLCache
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import sessionmaker

from nexus.core.exceptions import AuthenticationError
from nexus.storage.models import Base, OAuthCredentialModel
from nexus.storage.models.refresh_token_history import RefreshTokenHistoryModel

from .oauth_crypto import OAuthCrypto
from .oauth_provider import OAuthCredential, OAuthError, OAuthProvider

logger = logging.getLogger(__name__)

# Rate limit: minimum seconds between refresh attempts per credential
_REFRESH_COOLDOWN_SECONDS = 30

# Token cache TTL in seconds
_TOKEN_CACHE_TTL_SECONDS = 30

# Lock TTL: locks auto-expire after this many seconds of inactivity
_LOCK_TTL_SECONDS = 300

# Timeout for OAuth provider refresh calls (prevents indefinite lock holding)
_PROVIDER_REFRESH_TIMEOUT_SECONDS = 30

# History pruning: entries older than this are deleted on rotation
_HISTORY_RETENTION_DAYS = 30


def _hash_token(token: str) -> str:
    """Compute SHA-256 hash of a token (never store plaintext in history)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenManager:
    """Centralized OAuth token manager with automatic refresh and rotation.

    Manages OAuth credentials for all providers (Google, Microsoft, etc.).
    Provides automatic token refresh following MindsDB's pattern, with
    RFC 9700 refresh token rotation and reuse detection.

    Security features:
    - Encrypted token storage (Fernet)
    - Zone isolation
    - Immutable secrets audit logging
    - Refresh token rotation with reuse detection
    - Token family invalidation on reuse
    - Per-credential refresh rate limiting
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        db_url: str | None = None,
        encryption_key: str | None = None,
        audit_logger: Any | None = None,
        session_factory: Any | None = None,
    ):
        """Initialize token manager.

        Args:
            db_path: Path to SQLite database (deprecated, use db_url)
            db_url: Database URL
            encryption_key: Fernet encryption key (base64-encoded)
            audit_logger: Optional SecretsAuditLogger instance for audit trail
            session_factory: Optional SQLAlchemy sessionmaker. When provided,
                reuses the app-level connection pool instead of creating a
                separate engine. db_path/db_url are still needed for OAuthCrypto.
        """
        if session_factory is not None:
            self.SessionLocal = session_factory
            # Derive database_url for OAuthCrypto; engine is owned externally
            self.engine = session_factory.kw.get("bind") if hasattr(session_factory, "kw") else None
            self.database_url = db_url or (str(self.engine.url) if self.engine else "")
            self._owns_engine = False
        elif db_url:
            self.database_url = db_url
            self.engine = create_engine(
                self.database_url,
                connect_args={"check_same_thread": False} if "sqlite" in self.database_url else {},
            )
            self.SessionLocal = sessionmaker(bind=self.engine)
            Base.metadata.create_all(self.engine)
            self._owns_engine = True
        elif db_path:
            self.database_url = f"sqlite:///{db_path}"
            self.engine = create_engine(
                self.database_url,
                connect_args={"check_same_thread": False},
            )
            self.SessionLocal = sessionmaker(bind=self.engine)
            Base.metadata.create_all(self.engine)
            self._owns_engine = True
        else:
            raise ValueError("One of db_path, db_url, or session_factory must be provided")

        self.crypto = OAuthCrypto(encryption_key=encryption_key, db_url=self.database_url)
        self.providers: dict[str, OAuthProvider] = {}
        self._audit_logger = audit_logger

        # In-memory cache: (provider, user_email, zone_id) -> access_token
        self._token_cache: TTLCache[tuple[str, str, str], str] = TTLCache(
            maxsize=1024, ttl=_TOKEN_CACHE_TTL_SECONDS
        )

        # Per-credential asyncio lock prevents concurrent refresh races.
        # TTLCache auto-evicts stale locks to prevent unbounded memory growth.
        self._refresh_locks: TTLCache[tuple[str, str, str], asyncio.Lock] = TTLCache(
            maxsize=2048, ttl=_LOCK_TTL_SECONDS
        )

    def register_provider(self, provider_name: str, provider: OAuthProvider) -> None:
        """Register an OAuth provider."""
        self.providers[provider_name] = provider
        logger.info(f"Registered OAuth provider: {provider_name}")

    async def store_credential(
        self,
        provider: str,
        user_email: str,
        credential: OAuthCredential,
        zone_id: str = "default",
        created_by: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
    ) -> str:
        """Store OAuth credential in database with token family tracking.

        Returns:
            credential_id: Database credential ID
        """
        if not provider or not provider.strip():
            raise ValueError("Provider name cannot be empty")
        if zone_id is None:
            zone_id = "default"

        encrypted_access_token = self.crypto.encrypt_token(credential.access_token)
        encrypted_refresh_token = None
        refresh_token_hash = None
        if credential.refresh_token:
            encrypted_refresh_token = self.crypto.encrypt_token(credential.refresh_token)
            refresh_token_hash = _hash_token(credential.refresh_token)

        scopes_json = json.dumps(credential.scopes) if credential.scopes else None

        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.zone_id == zone_id,
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if user_id is None and created_by and created_by != user_email:
                user_id = created_by

            if existing:
                existing.encrypted_access_token = encrypted_access_token
                existing.encrypted_refresh_token = encrypted_refresh_token
                existing.token_type = credential.token_type
                existing.expires_at = credential.expires_at
                existing.scopes = scopes_json
                existing.client_id = credential.client_id
                existing.token_uri = credential.token_uri
                existing.user_id = user_id
                existing.updated_at = datetime.now(UTC)
                existing.revoked = 0
                # Reset rotation for updated credentials — new token family
                existing.token_family_id = str(uuid.uuid4())
                existing.rotation_counter = 0
                existing.refresh_token_hash = refresh_token_hash
                session.commit()

                logger.info(
                    f"Updated OAuth credential: {provider}:{user_email} (user_id={user_id})"
                )
                self._log_audit(
                    "credential_updated",
                    provider,
                    user_email,
                    zone_id,
                    credential_id=existing.credential_id,
                    token_family_id=existing.token_family_id,
                    ip_address=ip_address,
                )
                self._invalidate_cache(provider, user_email, zone_id)
                return str(existing.credential_id)
            else:
                token_family_id = str(uuid.uuid4())
                model = OAuthCredentialModel(
                    provider=provider,
                    user_email=user_email,
                    user_id=user_id,
                    zone_id=zone_id,
                    encrypted_access_token=encrypted_access_token,
                    encrypted_refresh_token=encrypted_refresh_token,
                    token_type=credential.token_type,
                    expires_at=credential.expires_at,
                    scopes=scopes_json,
                    client_id=credential.client_id,
                    token_uri=credential.token_uri,
                    created_by=created_by,
                    token_family_id=token_family_id,
                    rotation_counter=0,
                    refresh_token_hash=refresh_token_hash,
                )

                session.add(model)
                session.commit()
                session.refresh(model)

                logger.info(f"Stored OAuth credential: {provider}:{user_email}")
                self._log_audit(
                    "credential_created",
                    provider,
                    user_email,
                    zone_id,
                    credential_id=model.credential_id,
                    token_family_id=token_family_id,
                    ip_address=ip_address,
                )
                return model.credential_id

    async def get_valid_token(
        self,
        provider: str,
        user_email: str,
        zone_id: str = "default",
        ip_address: str | None = None,
    ) -> str:
        """Get a valid access token (with automatic refresh and rotation).

        Flow:
        1. Check in-memory cache
        2. Acquire per-credential lock (prevents concurrent refresh races)
        3. Double-check cache (another coroutine may have populated it)
        4. Retrieve credential from database
        5. Decrypt tokens
        6. Check if expired
        7. If expired: rate-limit check → refresh → reuse detection → rotate
        8. Return valid access_token
        """
        if zone_id is None:
            zone_id = "default"

        # Check cache first (fast path — no lock needed)
        cache_key = (provider, user_email, zone_id)
        cached = self._token_cache.get(cache_key)
        if cached is not None:
            return cached

        # Per-credential lock prevents concurrent refresh races.
        # Get-or-create pattern for TTLCache-based locks.
        lock = self._refresh_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[cache_key] = lock
        async with lock:
            # Double-check cache (another coroutine may have refreshed while we waited)
            cached = self._token_cache.get(cache_key)
            if cached is not None:
                return cached

            with self.SessionLocal() as session:
                stmt = select(OAuthCredentialModel).where(
                    OAuthCredentialModel.provider == provider,
                    OAuthCredentialModel.user_email == user_email,
                    OAuthCredentialModel.zone_id == zone_id,
                    OAuthCredentialModel.revoked == 0,
                )
                model = session.execute(stmt).scalar_one_or_none()

                if not model:
                    raise AuthenticationError(
                        f"No OAuth credential found for {provider}:{user_email}"
                    )

                credential = self._model_to_credential(model)

                refreshed = False
                rotated = False
                if credential.is_expired() and credential.refresh_token:
                    # Rate limit: skip if refreshed recently
                    if self._is_refresh_rate_limited(model):
                        logger.debug(
                            f"Refresh rate limited for {provider}:{user_email}, "
                            f"returning current token"
                        )
                        self._token_cache[cache_key] = credential.access_token
                        return credential.access_token

                    logger.info(f"Token expired for {provider}:{user_email}, refreshing...")

                    if provider not in self.providers:
                        raise AuthenticationError(f"Provider not registered: {provider}")

                    oauth_provider = self.providers[provider]

                    try:
                        try:
                            new_credential = await asyncio.wait_for(
                                oauth_provider.refresh_token(credential),
                                timeout=_PROVIDER_REFRESH_TIMEOUT_SECONDS,
                            )
                        except TimeoutError:
                            logger.error(f"OAuth refresh timed out for {provider}:{user_email}")
                            raise AuthenticationError(
                                f"OAuth refresh timed out for {provider}"
                            ) from None

                        encrypted_access_token = self.crypto.encrypt_token(
                            new_credential.access_token
                        )

                        model.encrypted_access_token = encrypted_access_token
                        model.expires_at = new_credential.expires_at
                        model.last_refreshed_at = datetime.now(UTC)
                        model.updated_at = datetime.now(UTC)

                        # Token rotation: check if provider returned a new refresh token
                        old_refresh_hash = model.refresh_token_hash
                        if (
                            new_credential.refresh_token
                            and new_credential.refresh_token != credential.refresh_token
                        ):
                            # Reuse detection: if the old hash is already in history,
                            # another caller already rotated it — our copy is stale
                            if old_refresh_hash and self._detect_reuse_in_session(
                                session, model.token_family_id, old_refresh_hash
                            ):
                                count = self._invalidate_family_in_session(
                                    session, model.token_family_id
                                )
                                session.commit()
                                self._invalidate_cache(provider, user_email, zone_id)
                                self._log_audit(
                                    "token_reuse_detected",
                                    provider,
                                    user_email,
                                    zone_id,
                                    credential_id=model.credential_id,
                                    token_family_id=model.token_family_id,
                                    details={
                                        "revoked_count": count,
                                        "reason": "refresh_token_reuse",
                                    },
                                    ip_address=ip_address,
                                )
                                raise AuthenticationError(
                                    "Refresh token reuse detected — "
                                    "token family invalidated for security"
                                )

                            # Provider rotated the refresh token — record the old one
                            self._record_rotation(session, model, old_refresh_hash)

                            encrypted_refresh_token = self.crypto.encrypt_token(
                                new_credential.refresh_token
                            )
                            model.encrypted_refresh_token = encrypted_refresh_token
                            model.refresh_token_hash = _hash_token(new_credential.refresh_token)
                            model.rotation_counter = (model.rotation_counter or 0) + 1

                            rotated = True
                            logger.info(
                                f"Refresh token rotated for {provider}:{user_email} "
                                f"(counter={model.rotation_counter})"
                            )
                        elif new_credential.refresh_token:
                            # Same refresh token returned — just update access token
                            encrypted_refresh_token = self.crypto.encrypt_token(
                                new_credential.refresh_token
                            )
                            model.encrypted_refresh_token = encrypted_refresh_token

                        # Prune old history entries periodically (every 10 rotations)
                        if (model.rotation_counter or 0) > 0 and (
                            model.rotation_counter or 0
                        ) % 10 == 0:
                            self._prune_history(session, model.token_family_id)

                        credential = new_credential
                        refreshed = True

                    except OAuthError as e:
                        logger.error(f"Failed to refresh token for {provider}:{user_email}: {e}")
                        raise AuthenticationError(f"Failed to refresh token: {e}") from e

                # Single commit: all credential updates + last_used_at
                model.last_used_at = datetime.now(UTC)
                session.commit()

                # Capture audit fields before session closes (avoids DetachedInstanceError)
                audit_credential_id = model.credential_id
                audit_family_id = model.token_family_id
                audit_rotation_counter = model.rotation_counter

                # Populate cache
                self._token_cache[cache_key] = credential.access_token

            # Audit logging AFTER session close — best-effort, non-blocking
            if refreshed:
                self._log_audit(
                    "token_refreshed",
                    provider,
                    user_email,
                    zone_id,
                    credential_id=audit_credential_id,
                    token_family_id=audit_family_id,
                    ip_address=ip_address,
                )
                # Log rotation event separately if refresh token actually changed
                if rotated:
                    self._log_audit(
                        "token_rotated",
                        provider,
                        user_email,
                        zone_id,
                        credential_id=audit_credential_id,
                        token_family_id=audit_family_id,
                        details={"rotation_counter": audit_rotation_counter},
                        ip_address=ip_address,
                    )

            return credential.access_token

    def detect_reuse(self, token_family_id: str, refresh_token_hash: str) -> bool:
        """Check if a refresh token hash exists in the rotation history.

        If it does, the token was already rotated out and this is a
        replay attack.  Returns True if reuse is detected.
        """
        with self.SessionLocal() as session:
            stmt = select(RefreshTokenHistoryModel).where(
                RefreshTokenHistoryModel.token_family_id == token_family_id,
                RefreshTokenHistoryModel.refresh_token_hash == refresh_token_hash,
            )
            result = session.execute(stmt).scalar_one_or_none()
            return result is not None

    def invalidate_family(self, token_family_id: str) -> int:
        """Invalidate all credentials in a token family.

        Called when token reuse is detected.  Revokes all credentials
        with the given token_family_id.

        Returns:
            Number of credentials revoked.
        """
        with self.SessionLocal() as session:
            now = datetime.now(UTC)
            result = session.execute(
                update(OAuthCredentialModel)
                .where(OAuthCredentialModel.token_family_id == token_family_id)
                .where(OAuthCredentialModel.revoked == 0)
                .values(revoked=1, revoked_at=now)
            )
            session.commit()
            count = result.rowcount or 0

            if count > 0:
                logger.warning(
                    f"SECURITY: Invalidated {count} credential(s) in family "
                    f"{token_family_id} due to refresh token reuse"
                )
                self._log_audit(
                    "family_invalidated",
                    "",
                    "",
                    "",
                    token_family_id=token_family_id,
                    details={"revoked_count": count, "reason": "refresh_token_reuse"},
                )

            return count

    async def get_credential(
        self, provider: str, user_email: str, zone_id: str = "default"
    ) -> OAuthCredential | None:
        """Get credential (decrypted) without automatic refresh."""
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.zone_id == zone_id,
                OAuthCredentialModel.revoked == 0,
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                return None

            return self._model_to_credential(model)

    async def revoke_credential(
        self,
        provider: str,
        user_email: str,
        zone_id: str = "default",
        ip_address: str | None = None,
    ) -> bool:
        """Revoke an OAuth credential."""
        if zone_id is None:
            zone_id = "default"
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.zone_id == zone_id,
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                return False

            if provider in self.providers:
                credential = self._model_to_credential(model)
                oauth_provider = self.providers[provider]
                try:
                    await oauth_provider.revoke_token(credential)
                except Exception as e:
                    logger.warning(f"Failed to revoke via provider API: {e}")

            # Capture audit fields before commit (avoids DetachedInstanceError)
            audit_credential_id = model.credential_id
            audit_family_id = model.token_family_id

            model.revoked = 1
            model.revoked_at = datetime.now(UTC)
            session.commit()

            logger.info(f"Revoked OAuth credential: {provider}:{user_email}")
            self._log_audit(
                "credential_revoked",
                provider,
                user_email,
                zone_id,
                credential_id=audit_credential_id,
                token_family_id=audit_family_id,
                ip_address=ip_address,
            )
            self._invalidate_cache(provider, user_email, zone_id)
            return True

    async def list_credentials(
        self,
        zone_id: str | None = None,
        user_email: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all credentials (metadata only, no tokens)."""
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(OAuthCredentialModel.revoked == 0)

            if zone_id is not None:
                stmt = stmt.where(OAuthCredentialModel.zone_id == zone_id)

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
                    "user_id": model.user_id,
                    "zone_id": model.zone_id,
                    "expires_at": model.expires_at.isoformat() if model.expires_at else None,
                    "is_expired": model.is_expired(),
                    "created_at": model.created_at.isoformat(),
                    "last_used_at": (
                        model.last_used_at.isoformat() if model.last_used_at else None
                    ),
                    "token_family_id": model.token_family_id,
                    "rotation_counter": model.rotation_counter,
                }
                for model in models
            ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _model_to_credential(self, model: OAuthCredentialModel) -> OAuthCredential:
        """Convert database model to OAuthCredential (decrypted)."""
        access_token = self.crypto.decrypt_token(model.encrypted_access_token)
        refresh_token = None
        if model.encrypted_refresh_token:
            refresh_token = self.crypto.decrypt_token(model.encrypted_refresh_token)

        scopes = None
        if model.scopes:
            scopes = json.loads(model.scopes)

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
        if model.user_id:
            if cred.metadata is None:
                cred.metadata = {}
            cred.metadata["user_id"] = model.user_id
        return cred

    def _is_refresh_rate_limited(self, model: OAuthCredentialModel) -> bool:
        """Check if this credential was refreshed too recently."""
        if model.last_refreshed_at is None:
            return False
        last = model.last_refreshed_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last < timedelta(seconds=_REFRESH_COOLDOWN_SECONDS)

    def _record_rotation(
        self,
        session: Any,
        model: OAuthCredentialModel,
        old_refresh_hash: str | None,
    ) -> None:
        """Record a retired refresh token in the history table."""
        if not old_refresh_hash:
            return

        history_entry = RefreshTokenHistoryModel(
            token_family_id=model.token_family_id or "",
            credential_id=model.credential_id,
            refresh_token_hash=old_refresh_hash,
            rotation_counter=model.rotation_counter or 0,
            zone_id=model.zone_id,
            rotated_at=datetime.now(UTC),
        )
        session.add(history_entry)

    def _detect_reuse_in_session(
        self,
        session: Any,
        token_family_id: str | None,
        refresh_token_hash: str,
    ) -> bool:
        """Check if a refresh token hash exists in rotation history (in-session).

        Same logic as ``detect_reuse()`` but uses the caller's open session
        instead of creating a new one — avoids nested transactions.
        """
        if not token_family_id:
            return False
        stmt = select(RefreshTokenHistoryModel).where(
            RefreshTokenHistoryModel.token_family_id == token_family_id,
            RefreshTokenHistoryModel.refresh_token_hash == refresh_token_hash,
        )
        return session.execute(stmt).scalar_one_or_none() is not None

    def _invalidate_family_in_session(
        self,
        session: Any,
        token_family_id: str | None,
    ) -> int:
        """Revoke all credentials in a token family (in-session).

        Same logic as ``invalidate_family()`` but uses the caller's open
        session — the caller is responsible for commit.
        """
        if not token_family_id:
            return 0
        now = datetime.now(UTC)
        result = session.execute(
            update(OAuthCredentialModel)
            .where(OAuthCredentialModel.token_family_id == token_family_id)
            .where(OAuthCredentialModel.revoked == 0)
            .values(revoked=1, revoked_at=now)
        )
        count = result.rowcount or 0
        if count > 0:
            logger.warning(
                f"SECURITY: Invalidated {count} credential(s) in family "
                f"{token_family_id} due to refresh token reuse (in-session)"
            )
        return count

    def _prune_history(self, session: Any, token_family_id: str | None) -> None:
        """Delete history entries older than _HISTORY_RETENTION_DAYS.

        Called within an open session — does NOT commit (caller handles that).
        """
        if not token_family_id:
            return
        cutoff = datetime.now(UTC) - timedelta(days=_HISTORY_RETENTION_DAYS)
        try:
            session.execute(
                delete(RefreshTokenHistoryModel).where(
                    RefreshTokenHistoryModel.token_family_id == token_family_id,
                    RefreshTokenHistoryModel.rotated_at < cutoff,
                )
            )
        except Exception:
            logger.warning("Failed to prune token history", exc_info=True)

    def _invalidate_cache(self, provider: str, user_email: str, zone_id: str) -> None:
        """Remove a token from the in-memory cache."""
        cache_key = (provider, user_email, zone_id)
        self._token_cache.pop(cache_key, None)

    def _log_audit(
        self,
        operation: str,
        provider: str,
        user_email: str,
        zone_id: str | None,
        *,
        credential_id: str | None = None,
        token_family_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Log audit trail for token operations.

        If a SecretsAuditLogger is configured, persists to the immutable
        audit log.  Always falls back to the Python logger.
        """
        logger.info(
            f"AUDIT: {operation} | provider={provider} | user={user_email} | zone={zone_id}"
        )

        if self._audit_logger is not None:
            try:
                self._audit_logger.log_event(
                    event_type=operation,
                    actor_id=user_email or "system",
                    provider=provider or None,
                    credential_id=credential_id,
                    token_family_id=token_family_id,
                    zone_id=zone_id or "default",
                    ip_address=ip_address,
                    details=details,
                )
            except Exception:
                logger.warning("Failed to write secrets audit log", exc_info=True)

    def close(self) -> None:
        """Cleanup resources."""
        if getattr(self, "_closed", False):
            return
        self._closed = True

        if not getattr(self, "_owns_engine", True) or self.engine is None:
            return

        try:
            self.engine.dispose()
        except Exception:
            logger.debug("Engine dispose failed (non-critical)", exc_info=True)
