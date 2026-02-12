"""Agent key service for Ed25519 cryptographic identity (KYA Phase 1, Issue #1355).

Manages the full lifecycle of agent Ed25519 key pairs:
- Key generation with JWK Thumbprint (RFC 7638) as key_id
- Fernet encryption of private keys (reuses OAuthCrypto)
- Public key lookup with TTLCache (300s TTL, 10k entries)
- Key rotation and revocation
- Identity verification for external systems

Design decisions:
- generate_key_pair() accepts an external session for transactional atomicity
  with AgentRegistry.register() (same DB transaction).
- JWK Thumbprint uses SHA-256 over canonical JSON per RFC 7638.
- Private keys are Fernet-encrypted before storage (AES-128-CBC + HMAC-SHA256).
- Public key cache is a dedicated TTLCache (not shared with AgentRegistry).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cachetools import TTLCache
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from sqlalchemy import select

from nexus.core.agent_key_record import AgentIdentityInfo, AgentKeyRecord
from nexus.storage.models.agent_key import AgentKeyModel
from nexus.storage.session_mixin import SessionMixin

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from nexus.server.auth.oauth_crypto import OAuthCrypto

logger = logging.getLogger(__name__)


def _base64url_encode(data: bytes) -> str:
    """Base64url encode without padding (RFC 7515 / RFC 7638)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _compute_jwk_thumbprint(public_key_jwk: dict) -> str:
    """Compute JWK Thumbprint per RFC 7638.

    For OKP keys: canonical JSON of {"crv", "kty", "x"} sorted by key name,
    then SHA-256 hash, then base64url encode.
    """
    canonical = {
        "crv": public_key_jwk["crv"],
        "kty": public_key_jwk["kty"],
        "x": public_key_jwk["x"],
    }
    canonical_json = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical_json.encode("ascii")).digest()
    return _base64url_encode(digest)


def _ed25519_to_jwk(public_key_bytes: bytes) -> dict:
    """Convert raw Ed25519 public key bytes to JWK dict (RFC 7517)."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _base64url_encode(public_key_bytes),
    }


class AgentKeyService(SessionMixin):
    """Ed25519 key lifecycle management for agent identity.

    Thread-safe: cache access synchronized via _cache_lock.

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
        crypto: OAuthCrypto instance for Fernet encryption of private keys.
        cache_maxsize: Max entries in the public key TTLCache.
        cache_ttl: TTL in seconds for cached public keys.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        crypto: OAuthCrypto,
        cache_maxsize: int = 10_000,
        cache_ttl: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._cache_lock = threading.Lock()
        self._key_cache: TTLCache[str, AgentKeyRecord | None] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl
        )

    def generate_key_pair(
        self,
        agent_id: str,
        zone_id: str | None,
        session: Session,
    ) -> AgentKeyRecord:
        """Generate an Ed25519 key pair and persist within the given session.

        This method accepts an external session so the key INSERT participates
        in the same transaction as agent registration (atomic commit).

        Args:
            agent_id: Agent to generate key for.
            zone_id: Zone for multi-zone isolation.
            session: Active SQLAlchemy session (caller manages commit/rollback).

        Returns:
            AgentKeyRecord snapshot of the generated key.
        """
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        public_key_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        private_key_bytes = private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )

        jwk = _ed25519_to_jwk(public_key_bytes)
        key_id = _compute_jwk_thumbprint(jwk)

        encrypted_private = self._crypto.encrypt_token(
            base64.b64encode(private_key_bytes).decode("ascii")
        )

        now = datetime.now(UTC)
        model = AgentKeyModel(
            key_id=key_id,
            agent_id=agent_id,
            zone_id=zone_id,
            algorithm="Ed25519",
            public_key_jwk=json.dumps(jwk),
            encrypted_private_key=encrypted_private,
            created_at=now,
            expires_at=None,
            revoked_at=None,
        )
        session.add(model)
        session.flush()

        record = AgentKeyRecord(
            key_id=key_id,
            agent_id=agent_id,
            zone_id=zone_id,
            algorithm="Ed25519",
            public_key_jwk=jwk,
            has_private_key=True,
            created_at=now,
            expires_at=None,
            revoked_at=None,
        )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[AGENT-KEY] Generated Ed25519 key %s for agent %s",
                key_id,
                agent_id,
            )

        return record

    def get_public_key(self, agent_id: str) -> AgentKeyRecord | None:
        """Get the active (non-revoked, non-expired) public key for an agent.

        Uses TTLCache for performance. Returns None if no active key exists.
        """
        with self._cache_lock:
            cached = self._key_cache.get(agent_id)
            if cached is not None:
                return cached

        with self._get_session() as session:
            model = (
                session.execute(
                    select(AgentKeyModel)
                    .where(AgentKeyModel.agent_id == agent_id)
                    .where(AgentKeyModel.revoked_at.is_(None))
                    .order_by(AgentKeyModel.created_at.desc())
                )
                .scalars()
                .first()
            )

            if model is None:
                return None

            record = self._model_to_record(model)

        if record.is_active:
            with self._cache_lock:
                self._key_cache[agent_id] = record
            return record
        return None

    def get_public_key_by_key_id(self, key_id: str) -> AgentKeyRecord | None:
        """Lookup a key by its JWK Thumbprint (for RFC 9421 keyid resolution)."""
        with self._get_session() as session:
            model = session.execute(
                select(AgentKeyModel).where(AgentKeyModel.key_id == key_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._model_to_record(model)

    def revoke_key(self, agent_id: str, key_id: str) -> bool:
        """Revoke a key by setting revoked_at. Invalidates cache.

        Returns True if key was found and revoked, False otherwise.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentKeyModel)
                .where(AgentKeyModel.key_id == key_id)
                .where(AgentKeyModel.agent_id == agent_id)
            ).scalar_one_or_none()

            if model is None:
                return False

            if model.revoked_at is not None:
                return False

            model.revoked_at = datetime.now(UTC)

        with self._cache_lock:
            self._key_cache.pop(agent_id, None)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[AGENT-KEY] Revoked key %s for agent %s", key_id, agent_id)
        return True

    def list_keys(self, agent_id: str, include_revoked: bool = False) -> list[AgentKeyRecord]:
        """List all keys for an agent, optionally including revoked ones."""
        with self._get_session() as session:
            stmt = select(AgentKeyModel).where(AgentKeyModel.agent_id == agent_id)
            if not include_revoked:
                stmt = stmt.where(AgentKeyModel.revoked_at.is_(None))
            stmt = stmt.order_by(AgentKeyModel.created_at.desc())

            models = list(session.execute(stmt).scalars().all())
            return [self._model_to_record(m) for m in models]

    def verify_identity(
        self, agent_id: str, owner_id: str, zone_id: str | None
    ) -> AgentIdentityInfo | None:
        """Build identity verification info for an agent.

        Returns None if agent has no active key.
        """
        key_record = self.get_public_key(agent_id)
        if key_record is None:
            return None

        return AgentIdentityInfo(
            agent_id=agent_id,
            owner_id=owner_id,
            zone_id=zone_id,
            key_id=key_record.key_id,
            algorithm=key_record.algorithm,
            public_key_jwk=key_record.public_key_jwk,
            created_at=key_record.created_at,
        )

    def rotate_key(self, agent_id: str, zone_id: str | None) -> AgentKeyRecord:
        """Generate a new key pair for an agent (key rotation).

        Manages its own session lifecycle. The old key is NOT automatically
        revoked (caller may want a grace period).

        Returns:
            AgentKeyRecord snapshot of the newly generated key.
        """
        with self._get_session() as session:
            return self.generate_key_pair(agent_id, zone_id, session)

    def delete_agent_keys(self, agent_id: str, session: Session) -> int:
        """Delete all keys for an agent within the given session.

        Used during agent unregistration for cascade cleanup.

        Returns:
            Number of keys deleted.
        """
        models = list(
            session.execute(select(AgentKeyModel).where(AgentKeyModel.agent_id == agent_id))
            .scalars()
            .all()
        )
        for m in models:
            session.delete(m)

        with self._cache_lock:
            self._key_cache.pop(agent_id, None)

        return len(models)

    @staticmethod
    def _model_to_record(model: AgentKeyModel) -> AgentKeyRecord:
        """Convert ORM model to frozen dataclass (never returns mutable ORM objects)."""
        jwk_dict = json.loads(model.public_key_jwk)
        return AgentKeyRecord(
            key_id=model.key_id,
            agent_id=model.agent_id,
            zone_id=model.zone_id,
            algorithm=model.algorithm,
            public_key_jwk=jwk_dict,
            has_private_key=model.encrypted_private_key is not None,
            created_at=model.created_at,
            expires_at=model.expires_at,
            revoked_at=model.revoked_at,
        )
