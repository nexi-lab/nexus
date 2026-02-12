"""Idempotent key management service (Issue #1355, Decision #8B).

KeyService manages Ed25519 signing keys with:
- Idempotent provisioning: ensure_keypair() is safe to call N times
- TTL cache for public keys (Decision #14B, 60s default)
- Cached revocation set (Decision #15B)
- Key rotation with grace period (Decision #16C)
- RFC 9421 keyid-based direct lookup

Thread-safe via threading.Lock for cache operations.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from cachetools import TTLCache
from sqlalchemy import select, update

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import create_did_key
from nexus.identity.models import AgentKeyModel


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime.

    SQLite stores naive datetimes, so comparisons must use naive UTC.
    PostgreSQL stores timezone-aware datetimes, but naive comparisons
    are also valid when the server timezone is UTC.
    """
    return datetime.now(UTC).replace(tzinfo=None)


if TYPE_CHECKING:
    from collections.abc import Generator

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentKeyRecord:
    """Immutable snapshot of an agent signing key."""

    key_id: str
    agent_id: str
    algorithm: str
    public_key_bytes: bytes
    did: str
    is_active: bool
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


# Sentinel for cache miss
_CACHE_MISS = object()


class KeyService:
    """Manages agent signing keys with idempotent provisioning.

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
        crypto: IdentityCrypto instance for key operations.
        cache_maxsize: Max entries in the public key TTL cache.
        cache_ttl: TTL in seconds for cached public keys (default: 60).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        crypto: IdentityCrypto,
        cache_maxsize: int = 5000,
        cache_ttl: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._lock = threading.Lock()

        # TTL cache: key_id -> (public_key_bytes, did, is_active)
        self._key_cache: TTLCache[str, AgentKeyRecord | None] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl
        )
        # TTL cache: agent_id -> list[AgentKeyRecord]
        self._agent_keys_cache: TTLCache[str, list[AgentKeyRecord]] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl
        )
        # Revocation cache (key_ids that are revoked) — bounded TTL cache
        self._revoked_cache: TTLCache[str, bool] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl * 5 if cache_ttl > 0 else 300
        )

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a session with auto-commit/rollback."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_keypair(self, agent_id: str) -> AgentKeyRecord:
        """Ensure an agent has at least one active keypair.

        Idempotent: if the agent already has an active key, returns it.
        Otherwise generates a new Ed25519 keypair, encrypts the private key,
        derives a did:key, and stores everything in the database.

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentKeyRecord for the active key (newest if multiple exist).

        Raises:
            ValueError: If agent_id is empty.
        """
        if not agent_id:
            raise ValueError("agent_id is required")

        # Check for existing active key (DB read)
        existing = self.get_active_keys(agent_id)
        if existing:
            return existing[0]  # Newest first

        # Generate new keypair
        private_key, public_key = self._crypto.generate_keypair()
        encrypted = self._crypto.encrypt_private_key(private_key)
        pub_bytes = IdentityCrypto.public_key_to_bytes(public_key)
        did = create_did_key(public_key)
        key_id = str(uuid.uuid4())
        now = _utcnow_naive()

        with self._get_session() as session:
            # Double-check inside transaction to prevent races (FOR UPDATE on PostgreSQL)
            existing_model = session.execute(
                select(AgentKeyModel)
                .where(AgentKeyModel.agent_id == agent_id)
                .where(AgentKeyModel.is_active == 1)
                .order_by(AgentKeyModel.created_at.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()

            if existing_model is not None:
                return self._model_to_record(existing_model)

            model = AgentKeyModel(
                key_id=key_id,
                agent_id=agent_id,
                algorithm="Ed25519",
                public_key_bytes=pub_bytes,
                encrypted_private_key=encrypted,
                did=did,
                is_active=1,
                created_at=now,
            )
            session.add(model)
            session.flush()

        # Invalidate caches for this agent
        self._invalidate_agent_cache(agent_id)

        logger.info(
            "[KYA] Generated keypair for agent %s (key_id=%s, did=%s)",
            agent_id,
            key_id,
            did,
        )

        return AgentKeyRecord(
            key_id=key_id,
            agent_id=agent_id,
            algorithm="Ed25519",
            public_key_bytes=pub_bytes,
            did=did,
            is_active=True,
            created_at=now,
            expires_at=None,
            revoked_at=None,
        )

    def get_active_keys(self, agent_id: str) -> list[AgentKeyRecord]:
        """Get all active keys for an agent, newest first.

        Uses TTL cache to avoid per-request DB hits.

        Args:
            agent_id: Agent identifier.

        Returns:
            List of active AgentKeyRecord snapshots, newest first.
        """
        with self._lock:
            cached = self._agent_keys_cache.get(agent_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cast(list[AgentKeyRecord], cached)

        now = _utcnow_naive()
        with self._get_session() as session:
            models = list(
                session.execute(
                    select(AgentKeyModel)
                    .where(AgentKeyModel.agent_id == agent_id)
                    .where(AgentKeyModel.is_active == 1)
                    .order_by(AgentKeyModel.created_at.desc())
                )
                .scalars()
                .all()
            )

            # Filter out expired keys
            records = []
            for m in models:
                if m.expires_at is not None and m.expires_at < now:
                    continue
                records.append(self._model_to_record(m))

        with self._lock:
            self._agent_keys_cache[agent_id] = records

        return records

    def get_public_key(self, key_id: str) -> AgentKeyRecord | None:
        """Lookup a key by key_id (for RFC 9421 keyid parameter).

        Uses TTL cache for fast repeated lookups.

        Args:
            key_id: UUID key identifier.

        Returns:
            AgentKeyRecord if found and active, None otherwise.
        """
        with self._lock:
            cached = self._key_cache.get(key_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cast("AgentKeyRecord | None", cached)

        with self._get_session() as session:
            model = session.execute(
                select(AgentKeyModel).where(AgentKeyModel.key_id == key_id)
            ).scalar_one_or_none()

            if model is None:
                with self._lock:
                    self._key_cache[key_id] = None
                return None

            record = self._model_to_record(model)

        with self._lock:
            self._key_cache[key_id] = record

        return record

    def get_public_key_object(self, key_id: str) -> Ed25519PublicKey | None:
        """Get the Ed25519PublicKey object for a key_id.

        Convenience method for signature verification.

        Args:
            key_id: UUID key identifier.

        Returns:
            Ed25519PublicKey if found and active, None otherwise.
        """
        record = self.get_public_key(key_id)
        if record is None or not record.is_active:
            return None

        # Check expiry
        if record.expires_at is not None and record.expires_at < _utcnow_naive():
            return None

        return IdentityCrypto.public_key_from_bytes(record.public_key_bytes)

    def rotate_key(
        self,
        agent_id: str,
        grace_period_hours: int = 24,
    ) -> AgentKeyRecord:
        """Generate a new key and mark old keys with expiry.

        The old key remains valid during the grace period (Decision #16C).
        After expiry, the old key is no longer valid for verification.

        Args:
            agent_id: Agent identifier.
            grace_period_hours: Hours before old keys expire (default: 24).

        Returns:
            AgentKeyRecord for the new key.
        """
        now = _utcnow_naive()
        expires_at = now + timedelta(hours=grace_period_hours)

        # Generate new keypair before transaction (crypto ops outside DB lock)
        private_key, public_key = self._crypto.generate_keypair()
        encrypted = self._crypto.encrypt_private_key(private_key)
        pub_bytes = IdentityCrypto.public_key_to_bytes(public_key)
        did = create_did_key(public_key)
        key_id = str(uuid.uuid4())

        # Atomic: mark old keys with expiry AND insert new key in ONE transaction
        with self._get_session() as session:
            session.execute(
                update(AgentKeyModel)
                .where(AgentKeyModel.agent_id == agent_id)
                .where(AgentKeyModel.is_active == 1)
                .where(AgentKeyModel.expires_at.is_(None))
                .values(expires_at=expires_at)
            )
            model = AgentKeyModel(
                key_id=key_id,
                agent_id=agent_id,
                algorithm="Ed25519",
                public_key_bytes=pub_bytes,
                encrypted_private_key=encrypted,
                did=did,
                is_active=1,
                created_at=now,
            )
            session.add(model)
            session.flush()

        self._invalidate_agent_cache(agent_id)

        logger.info(
            "[KYA] Rotated key for agent %s (new_key=%s, old_keys_expire=%s)",
            agent_id,
            key_id,
            expires_at.isoformat(),
        )

        return AgentKeyRecord(
            key_id=key_id,
            agent_id=agent_id,
            algorithm="Ed25519",
            public_key_bytes=pub_bytes,
            did=did,
            is_active=True,
            created_at=now,
            expires_at=None,
            revoked_at=None,
        )

    def revoke_key(self, key_id: str) -> bool:
        """Immediately revoke a key.

        Sets is_active=0, revoked_at=now. Invalidates all caches.

        Args:
            key_id: UUID of the key to revoke.

        Returns:
            True if the key was found and revoked, False if not found.
        """
        now = _utcnow_naive()

        with self._get_session() as session:
            model = session.execute(
                select(AgentKeyModel).where(AgentKeyModel.key_id == key_id)
            ).scalar_one_or_none()

            if model is None:
                return False

            agent_id = model.agent_id

            session.execute(
                update(AgentKeyModel)
                .where(AgentKeyModel.key_id == key_id)
                .values(is_active=0, revoked_at=now)
            )

        # Update revocation cache
        with self._lock:
            self._revoked_cache[key_id] = True

        # Invalidate caches
        self._invalidate_key_cache(key_id)
        self._invalidate_agent_cache(agent_id)

        logger.info("[KYA] Revoked key %s for agent %s", key_id, agent_id)
        return True

    def is_revoked(self, key_id: str) -> bool:
        """Check if a key is revoked. Uses cached revocation set.

        Args:
            key_id: UUID of the key to check.

        Returns:
            True if the key is revoked.
        """
        with self._lock:
            if key_id in self._revoked_cache:
                return True

        # Check DB if not in cache
        record = self.get_public_key(key_id)
        if record is not None and record.revoked_at is not None:
            with self._lock:
                self._revoked_cache[key_id] = True
            return True

        return False

    def decrypt_private_key(self, key_id: str) -> Any:
        """Decrypt and return the private key for signing operations.

        Args:
            key_id: UUID of the key.

        Returns:
            Ed25519PrivateKey instance.

        Raises:
            ValueError: If key not found or revoked.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentKeyModel).where(AgentKeyModel.key_id == key_id)
            ).scalar_one_or_none()

            if model is None:
                raise ValueError(f"Key '{key_id}' not found")

            if not model.is_active:
                raise ValueError(f"Key '{key_id}' is not active")

            return self._crypto.decrypt_private_key(model.encrypted_private_key)

    def _invalidate_agent_cache(self, agent_id: str) -> None:
        """Invalidate agent-level caches."""
        with self._lock:
            self._agent_keys_cache.pop(agent_id, None)

    def _invalidate_key_cache(self, key_id: str) -> None:
        """Invalidate key-level cache."""
        with self._lock:
            self._key_cache.pop(key_id, None)

    @staticmethod
    def _model_to_record(model: AgentKeyModel) -> AgentKeyRecord:
        """Convert ORM model to frozen dataclass."""
        return AgentKeyRecord(
            key_id=model.key_id,
            agent_id=model.agent_id,
            algorithm=model.algorithm,
            public_key_bytes=bytes(model.public_key_bytes),
            did=model.did,
            is_active=bool(model.is_active),
            created_at=model.created_at,
            expires_at=model.expires_at,
            revoked_at=model.revoked_at,
        )
