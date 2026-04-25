"""Centralized OAuth token management with automatic refresh and rotation.

Canonical location: ``nexus.bricks.auth.oauth.token_manager`` (Issue #2281).
Moved from ``nexus.server.auth.token_manager``.

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
- CacheStoreABC-based token caching (30s TTL)
- Per-credential refresh rate limiting (30s cooldown)

Issue #997: OAuth token rotation and secrets audit logging.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

from nexus.bricks.auth.oauth.crypto import OAuthCrypto
from nexus.bricks.auth.oauth.token_resolver import ResolvedToken
from nexus.bricks.auth.oauth.types import OAuthCredential, OAuthError
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import AuthenticationError
from nexus.storage.models import OAuthCredentialModel
from nexus.storage.token_rotation_store import TokenRotationStore

logger = logging.getLogger(__name__)

# Rate limit: minimum seconds between refresh attempts per credential
_REFRESH_COOLDOWN_SECONDS = 30

# Token cache TTL in seconds
_TOKEN_CACHE_TTL_SECONDS = 60
_TOKEN_CACHE_EXPIRY_SAFETY_SECONDS = 60

# Timeout for OAuth provider refresh calls (prevents indefinite lock holding)
_PROVIDER_REFRESH_TIMEOUT_SECONDS = 30

# History pruning: entries older than this are deleted on rotation
_HISTORY_RETENTION_DAYS = 30

# Lock acquisition timeout (prevents indefinite wait on contended locks)
_LOCK_ACQUIRE_TIMEOUT_SECONDS = 60

# Maximum number of per-credential locks kept in memory (LRU eviction)
_MAX_REFRESH_LOCKS = 1024


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
        cache_store: "CacheStoreABC | None" = None,
    ):
        """Initialize token manager.

        Args:
            db_path: Path to SQLite database.
            db_url: Database URL.
            encryption_key: Fernet encryption key (base64-encoded).
            audit_logger: Optional SecretsAuditLogger instance for audit trail.
            session_factory: Optional SQLAlchemy sessionmaker. When provided,
                reuses the app-level connection pool instead of creating a
                separate engine (Issue #1597).
            cache_store: CacheStoreABC for token caching (optional, degrades gracefully).
        """
        if session_factory is not None:
            self.SessionLocal = session_factory
            # Derive database_url for OAuthCrypto; engine is owned externally
            self.engine = session_factory.kw.get("bind") if hasattr(session_factory, "kw") else None
            self.database_url = db_url or (str(self.engine.url) if self.engine else "")
            self._owns_engine = False
        elif db_url or db_path:
            from nexus.storage.record_store import SQLAlchemyRecordStore

            url = db_url or f"sqlite:///{db_path}"
            self._record_store = SQLAlchemyRecordStore(db_url=url)
            self.SessionLocal = self._record_store.session_factory
            self.engine = self._record_store.engine
            self.database_url = url
            self._owns_engine = True
        else:
            raise ValueError("One of db_path, db_url, or session_factory must be provided")

        # OAuthCrypto: use explicit key or fall back to random (no settings_store here;
        # callers that need persistent keys should pass encryption_key).
        self.crypto = OAuthCrypto(encryption_key=encryption_key)
        self.providers: dict[str, Any] = {}
        self._audit_logger = audit_logger
        self._rotation_store = TokenRotationStore()

        # CacheStoreABC for token caching (degrades gracefully when None)
        self._cache_store = cache_store
        self._cache_ttl = _TOKEN_CACHE_TTL_SECONDS

        # Per-credential asyncio lock prevents concurrent refresh races.
        # Capped at _MAX_REFRESH_LOCKS entries; oldest evicted on overflow (Issue #2281).
        self._refresh_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

        # Per-key metadata stash for resolve(): populated inside
        # get_valid_token()'s locked section. Keyed by (provider, user_email,
        # zone_id) so different credentials never cross-contaminate.
        self._resolved_metadata: dict[
            tuple[str, str, str], tuple[datetime | None, tuple[str, ...] | None]
        ] = {}

    def _get_refresh_lock(self, key: tuple[str, str, str]) -> asyncio.Lock:
        """Get or create a per-credential lock with LRU eviction (Issue #2281).

        When the lock dict exceeds _MAX_REFRESH_LOCKS, the oldest entries
        (those not currently held) are evicted to prevent unbounded growth.
        """
        lock = self._refresh_locks.get(key)
        if lock is not None:
            # Move to end (LRU refresh) by re-inserting
            self._refresh_locks.pop(key, None)
            self._refresh_locks[key] = lock
            return lock

        # Evict oldest unlocked entries if at capacity
        if len(self._refresh_locks) >= _MAX_REFRESH_LOCKS:
            to_remove = []
            for k, v in self._refresh_locks.items():
                if not v.locked():
                    to_remove.append(k)
                if len(self._refresh_locks) - len(to_remove) < _MAX_REFRESH_LOCKS:
                    break
            for k in to_remove:
                del self._refresh_locks[k]

        lock = asyncio.Lock()
        self._refresh_locks[key] = lock
        return lock

    def _token_cache_key(self, provider: str, user_email: str, zone_id: str) -> str:
        """Zone-scoped cache key for OAuth token caching."""
        return f"oauth:token:{provider}:{user_email}:{zone_id}"

    def _legacy_token_cache_key(self, provider: str, user_email: str, zone_id: str) -> str:
        """Legacy cache key used before structured token metadata was added."""
        return self._token_cache_key(provider, user_email, zone_id)

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _encode_cached_token(
        cls,
        credential: OAuthCredential,
        *,
        credential_id: str | None = None,
        token_family_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> bytes:
        del credential_id, token_family_id, updated_at
        return credential.access_token.encode("utf-8")

    @classmethod
    def _decode_cached_token(
        cls, payload: bytes
    ) -> tuple[str, datetime | None, tuple[str, ...] | None] | None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                return payload.decode("utf-8"), None, None
            except UnicodeDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return None

        expires_at_raw = data.get("expires_at")
        if expires_at_raw is None:
            expires_at = None
        elif isinstance(expires_at_raw, str):
            try:
                expires_at = cls._normalize_utc(datetime.fromisoformat(expires_at_raw))
            except ValueError:
                return None
        else:
            return None

        scopes_raw = data.get("scopes")
        scopes = tuple(scopes_raw) if isinstance(scopes_raw, list) else None
        return access_token, expires_at, scopes

    @classmethod
    def _decode_cached_generation(
        cls, payload: bytes
    ) -> tuple[str | None, str | None, datetime | None] | None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        updated_at_raw = data.get("updated_at")
        if updated_at_raw is None:
            updated_at = None
        elif isinstance(updated_at_raw, str):
            try:
                updated_at = cls._normalize_utc(datetime.fromisoformat(updated_at_raw))
            except ValueError:
                return None
        else:
            return None
        credential_id = data.get("credential_id")
        token_family_id = data.get("token_family_id")
        if credential_id is not None and not isinstance(credential_id, str):
            return None
        if token_family_id is not None and not isinstance(token_family_id, str):
            return None
        return credential_id, token_family_id, updated_at

    @classmethod
    def _is_cached_token_usable(cls, expires_at: datetime | None) -> bool:
        normalized_expiry = cls._normalize_utc(expires_at)
        if normalized_expiry is None:
            return True
        safe_expiry = normalized_expiry - timedelta(seconds=_TOKEN_CACHE_EXPIRY_SAFETY_SECONDS)
        return datetime.now(UTC) < safe_expiry

    def _cache_ttl_for(self, credential: OAuthCredential) -> int | None:
        normalized_expiry = self._normalize_utc(credential.expires_at)
        if normalized_expiry is None:
            return self._cache_ttl
        safe_expiry = normalized_expiry - timedelta(seconds=_TOKEN_CACHE_EXPIRY_SAFETY_SECONDS)
        remaining = int((safe_expiry - datetime.now(UTC)).total_seconds())
        if remaining < 1:
            return None
        return min(self._cache_ttl, remaining)

    async def _get_cached_token(
        self,
        cache_key: str,
        *,
        metadata_key: tuple[str, str, str] | None = None,
    ) -> str | None:
        if self._cache_store is None:
            return None
        cached_raw = await self._cache_store.get(cache_key)
        if cached_raw is None:
            return None
        cached = self._decode_cached_token(cached_raw)
        if cached is None:
            await self._cache_store.delete(cache_key)
            return None
        access_token, expires_at, scopes = cached
        if not self._is_cached_token_usable(expires_at):
            await self._cache_store.delete(cache_key)
            return None
        if metadata_key is not None:
            self._resolved_metadata[metadata_key] = (expires_at, scopes)
        return access_token

    async def _get_legacy_cached_entry(
        self,
        cache_key: str,
    ) -> bytes | None:
        if self._cache_store is None:
            return None
        return await self._cache_store.get(cache_key)

    def register_provider(self, provider_name: str, provider: Any) -> None:
        """Register an OAuth provider."""
        self.providers[provider_name] = provider
        logger.info(f"Registered OAuth provider: {provider_name}")

    async def store_credential(
        self,
        provider: str,
        user_email: str,
        credential: OAuthCredential,
        zone_id: str = ROOT_ZONE_ID,
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
            zone_id = ROOT_ZONE_ID

        encrypted_access_token = self.crypto.encrypt_token(credential.access_token)
        encrypted_refresh_token = None
        refresh_token_hash = None
        if credential.refresh_token:
            encrypted_refresh_token = self.crypto.encrypt_token(credential.refresh_token)
            refresh_token_hash = _hash_token(credential.refresh_token)

        scopes_json = json.dumps(credential.scopes) if credential.scopes else None
        lock_key = (provider, user_email, zone_id)
        lock = self._get_refresh_lock(lock_key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise AuthenticationError(
                f"Token refresh lock acquisition timed out for {provider}:{user_email}"
            ) from None
        try:
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
                    existing.revoked_at = None
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
                    await self._invalidate_cache(provider, user_email, zone_id)
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
        finally:
            lock.release()

    async def get_valid_token(
        self,
        provider: str,
        user_email: str,
        zone_id: str = ROOT_ZONE_ID,
        ip_address: str | None = None,
    ) -> str:
        """Get a valid access token (with automatic refresh and rotation).

        Flow:
        1. Check CacheStoreABC cache
        2. Acquire per-credential lock (prevents concurrent refresh races)
        3. Double-check cache (another coroutine may have populated it)
        4. Retrieve credential from database
        5. Decrypt tokens
        6. Check if expired
        7. If expired: rate-limit check → refresh → reuse detection → rotate
        8. Return valid access_token
        """
        if zone_id is None:
            zone_id = ROOT_ZONE_ID

        metadata_key = (provider, user_email, zone_id)
        cache_key_str = self._token_cache_key(provider, user_email, zone_id)

        # Per-credential lock prevents concurrent refresh races (Issue #2281).
        # _last_resolved stash: resolve() reads metadata from the same locked
        # section that produced the access token, avoiding a second DB round-trip.
        lock_key = metadata_key
        lock = self._get_refresh_lock(lock_key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise AuthenticationError(
                f"Token refresh lock acquisition timed out for {provider}:{user_email}"
            ) from None
        try:
            cached_raw = await self._get_legacy_cached_entry(cache_key_str)
            cached_token = None
            if cached_raw is not None:
                try:
                    cached_token = cached_raw.decode("utf-8")
                except UnicodeDecodeError:
                    if self._cache_store is not None:
                        await self._cache_store.delete(cache_key_str)
                    cached_raw = None

            with self.SessionLocal() as session:
                stmt = (
                    select(OAuthCredentialModel)
                    .where(
                        OAuthCredentialModel.provider == provider,
                        OAuthCredentialModel.user_email == user_email,
                        OAuthCredentialModel.zone_id == zone_id,
                        OAuthCredentialModel.revoked == 0,
                    )
                    .order_by(OAuthCredentialModel.created_at.desc())
                )
                model = session.execute(stmt).scalar_one_or_none()

                if not model:
                    if cached_raw is not None and self._cache_store is not None:
                        await self._cache_store.delete(cache_key_str)
                    raise AuthenticationError(
                        f"No OAuth credential found for {provider}:{user_email}"
                    )

                credential = self._model_to_credential(model)

                if (
                    cached_token is not None
                    and cached_token == credential.access_token
                    and not credential.is_expired()
                ):
                    self._resolved_metadata[metadata_key] = (
                        credential.expires_at,
                        credential.scopes,
                    )
                    return cached_token
                if cached_raw is not None and self._cache_store is not None:
                    await self._cache_store.delete(cache_key_str)

                refreshed = False
                rotated = False
                if credential.is_expired() and credential.refresh_token:
                    # Rate limit: skip if refreshed recently
                    if self._is_refresh_rate_limited(model):
                        raise AuthenticationError(
                            f"Token expired for {provider}:{user_email} and "
                            f"refresh is rate-limited (cooldown {_REFRESH_COOLDOWN_SECONDS}s)"
                        )

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
                        if new_credential.scopes is not None:
                            model.scopes = json.dumps(list(new_credential.scopes))
                        elif credential.scopes is not None:
                            new_credential = replace(new_credential, scopes=credential.scopes)
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
                            if old_refresh_hash and self._rotation_store.detect_reuse(
                                session, model.token_family_id, old_refresh_hash
                            ):
                                count = self._rotation_store.invalidate_family(
                                    session, model.token_family_id
                                )
                                session.commit()
                                await self._invalidate_cache(provider, user_email, zone_id)
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
                            self._rotation_store.record_rotation(
                                session,
                                credential_id=model.credential_id,
                                token_family_id=model.token_family_id or "",
                                refresh_token_hash=old_refresh_hash,
                                rotation_counter=model.rotation_counter or 0,
                                zone_id=model.zone_id,
                            )

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
                            self._rotation_store.prune_history(
                                session,
                                model.token_family_id,
                                retention_days=_HISTORY_RETENTION_DAYS,
                            )

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

            if credential.is_expired():
                raise AuthenticationError(
                    f"Token expired for {provider}:{user_email} and no refresh token available"
                )

            self._resolved_metadata[(provider, user_email, zone_id)] = (
                credential.expires_at,
                credential.scopes,
            )
            if self._cache_store is not None:
                cache_ttl = self._cache_ttl_for(credential)
                if cache_ttl is not None:
                    await self._cache_store.set(
                        cache_key_str,
                        self._encode_cached_token(credential),
                        ttl=cache_ttl,
                    )
            return credential.access_token
        finally:
            lock.release()

    async def resolve(
        self,
        provider: str,
        user_email: str,
        *,
        zone_id: str = ROOT_ZONE_ID,
    ) -> ResolvedToken:
        """Resolve a valid access token via the ``TokenResolver`` seam.

        Reads metadata from a per-key stash populated inside
        ``get_valid_token()``'s locked section. On a fresh worker backed
        by an external cache (Dragonfly/Redis), the stash may be empty
        because ``get_valid_token()`` returned from a cache hit without
        touching the DB. In that case we fall back to ``get_credential()``
        for the metadata — one extra read, but only on the first call per
        credential per worker lifetime.
        """
        if zone_id is None:
            zone_id = ROOT_ZONE_ID
        access_token = await self.get_valid_token(provider, user_email, zone_id=zone_id)
        key = (provider, user_email, zone_id)
        metadata = self._resolved_metadata.get(key)
        if metadata is not None:
            expires_at, scopes = metadata
        else:
            credential = await self.get_credential(provider, user_email, zone_id=zone_id)
            if credential is not None:
                expires_at = credential.expires_at
                scopes = credential.scopes
                self._resolved_metadata[key] = (expires_at, scopes)
            else:
                expires_at = None
                scopes = None
        return ResolvedToken(
            access_token=access_token,
            expires_at=expires_at,
            scopes=scopes or (),
        )

    def detect_reuse(self, token_family_id: str, refresh_token_hash: str) -> bool:
        """Check if a refresh token hash exists in the rotation history.

        If it does, the token was already rotated out and this is a
        replay attack.  Returns True if reuse is detected.
        """
        with self.SessionLocal() as session:
            return self._rotation_store.detect_reuse(session, token_family_id, refresh_token_hash)

    def invalidate_family(self, token_family_id: str) -> int:
        """Invalidate all credentials in a token family.

        Called when token reuse is detected.  Revokes all credentials
        with the given token_family_id.

        Returns:
            Number of credentials revoked.
        """
        with self.SessionLocal() as session:
            count = self._rotation_store.invalidate_family(session, token_family_id)
            session.commit()

            if count > 0:
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
        self, provider: str, user_email: str, zone_id: str = ROOT_ZONE_ID
    ) -> OAuthCredential | None:
        """Get credential (decrypted) without automatic refresh."""
        with self.SessionLocal() as session:
            stmt = (
                select(OAuthCredentialModel)
                .where(
                    OAuthCredentialModel.provider == provider,
                    OAuthCredentialModel.user_email == user_email,
                    OAuthCredentialModel.zone_id == zone_id,
                    OAuthCredentialModel.revoked == 0,
                )
                .order_by(OAuthCredentialModel.created_at.desc())
            )
            model = session.execute(stmt).scalar_one_or_none()

            if not model:
                return None

            return self._model_to_credential(model)

    async def revoke_credential(
        self,
        provider: str,
        user_email: str,
        zone_id: str = ROOT_ZONE_ID,
        ip_address: str | None = None,
    ) -> bool:
        """Revoke an OAuth credential."""
        if zone_id is None:
            zone_id = ROOT_ZONE_ID
        lock_key = (provider, user_email, zone_id)
        lock = self._get_refresh_lock(lock_key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise AuthenticationError(
                f"Token refresh lock acquisition timed out for {provider}:{user_email}"
            ) from None
        try:
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
                    oauth_provider = self.providers[provider]
                    try:
                        await asyncio.wait_for(
                            oauth_provider.revoke_token(self._model_to_credential(model)),
                            timeout=_PROVIDER_REFRESH_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning(
                            "Timed out revoking provider token for %s:%s",
                            provider,
                            user_email,
                        )
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
                await self._invalidate_cache(provider, user_email, zone_id)
                return True
        finally:
            lock.release()

    async def list_credentials(
        self,
        zone_id: str | None = None,
        user_email: str | None = None,
        user_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """List all credentials (metadata only, no tokens)."""
        with self.SessionLocal() as session:
            stmt = select(OAuthCredentialModel)

            if not include_revoked:
                stmt = stmt.where(OAuthCredentialModel.revoked == 0)

            if zone_id is not None:
                stmt = stmt.where(OAuthCredentialModel.zone_id == zone_id)

            if user_id is not None:
                stmt = stmt.where(OAuthCredentialModel.user_id == user_id)
            elif user_email is not None:
                stmt = stmt.where(OAuthCredentialModel.user_email == user_email)

            models = (
                session.execute(stmt.order_by(OAuthCredentialModel.created_at.desc()))
                .scalars()
                .all()
            )

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
                    "revoked": bool(model.revoked),
                    "revoked_at": model.revoked_at.isoformat() if model.revoked_at else None,
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

        scopes: tuple[str, ...] | None = None
        if model.scopes:
            scopes = tuple(json.loads(model.scopes))

        expires_at = model.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        metadata: dict[str, Any] | None = None
        if model.user_id:
            metadata = {"user_id": model.user_id}

        return OAuthCredential(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=model.token_type,
            expires_at=expires_at,
            scopes=scopes,
            provider=model.provider,
            user_email=model.user_email,
            client_id=model.client_id,
            token_uri=model.token_uri,
            metadata=metadata,
        )

    def _is_refresh_rate_limited(self, model: OAuthCredentialModel) -> bool:
        """Check if this credential was refreshed too recently."""
        if model.last_refreshed_at is None:
            return False
        last = model.last_refreshed_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last < timedelta(seconds=_REFRESH_COOLDOWN_SECONDS)

    async def _invalidate_cache(self, provider: str, user_email: str, zone_id: str) -> None:
        """Remove a token from the cache."""
        if self._cache_store is not None:
            await self._cache_store.delete(self._token_cache_key(provider, user_email, zone_id))
            await self._cache_store.delete(
                self._legacy_token_cache_key(provider, user_email, zone_id)
            )

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
                    zone_id=zone_id or ROOT_ZONE_ID,
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

        if not getattr(self, "_owns_engine", True):
            return

        record_store = getattr(self, "_record_store", None)
        if record_store is not None:
            try:
                record_store.close()
            except Exception:
                logger.debug("RecordStore close failed (non-critical)", exc_info=True)
        elif self.engine is not None:
            try:
                self.engine.dispose()
            except Exception:
                logger.debug("Engine dispose failed (non-critical)", exc_info=True)
