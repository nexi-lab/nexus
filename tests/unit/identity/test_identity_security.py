"""Security tests for identity operations (Issue #1355, Decision #11B).

Tests OWASP Agentic Top 10 (ASI03) attack vectors:
- Expired key rejection
- Revoked credential rejection
- Cross-agent impersonation
- Key rotation grace period boundaries
- DID mismatch detection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import resolve_did_key
from nexus.identity.key_service import KeyService
from nexus.identity.models import AgentKeyModel
from nexus.storage.models import Base


def _utcnow_naive() -> datetime:
    """UTC naive datetime for tests — matches key_service._utcnow_naive()."""
    return datetime.now(UTC).replace(tzinfo=None)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Any:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine: Any) -> Any:
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_oauth_crypto() -> MagicMock:
    mock = MagicMock()
    _store: dict[str, str] = {}

    def encrypt(token: str) -> str:
        enc = f"ENC:{token}"
        _store[enc] = token
        return enc

    def decrypt(enc: str) -> str:
        return _store[enc]

    mock.encrypt_token.side_effect = encrypt
    mock.decrypt_token.side_effect = decrypt
    return mock


@pytest.fixture
def crypto(mock_oauth_crypto: MagicMock) -> IdentityCrypto:
    return IdentityCrypto(oauth_crypto=mock_oauth_crypto)


@pytest.fixture
def key_service(session_factory: Any, crypto: IdentityCrypto) -> KeyService:
    return KeyService(session_factory=session_factory, crypto=crypto, cache_ttl=0)


# ---------------------------------------------------------------------------
# Expired Key Rejection
# ---------------------------------------------------------------------------


class TestExpiredKeyRejection:
    def test_expired_key_not_in_active_keys(
        self, key_service: KeyService, session_factory: Any
    ) -> None:
        """Keys past their expires_at are filtered from active key queries."""
        record = key_service.ensure_keypair("agent-1")

        # Manually expire the key in the past
        with session_factory() as session:
            session.execute(
                update(AgentKeyModel)
                .where(AgentKeyModel.key_id == record.key_id)
                .values(expires_at=_utcnow_naive() - timedelta(hours=1))
            )
            session.commit()

        # Cache is TTL=0, so it will re-query
        keys = key_service.get_active_keys("agent-1")
        assert len(keys) == 0

    def test_expired_key_returns_none_from_get_public_key_object(
        self, key_service: KeyService, session_factory: Any
    ) -> None:
        """get_public_key_object returns None for expired keys."""
        record = key_service.ensure_keypair("agent-1")

        with session_factory() as session:
            session.execute(
                update(AgentKeyModel)
                .where(AgentKeyModel.key_id == record.key_id)
                .values(expires_at=_utcnow_naive() - timedelta(hours=1))
            )
            session.commit()

        assert key_service.get_public_key_object(record.key_id) is None


# ---------------------------------------------------------------------------
# Cross-Agent Impersonation
# ---------------------------------------------------------------------------


class TestCrossAgentImpersonation:
    def test_agent_a_cannot_sign_as_agent_b(
        self, key_service: KeyService, crypto: IdentityCrypto
    ) -> None:
        """Agent A's private key cannot create valid signatures for Agent B."""
        record_a = key_service.ensure_keypair("agent-a")
        record_b = key_service.ensure_keypair("agent-b")

        # Get Agent A's private key
        private_a = key_service.decrypt_private_key(record_a.key_id)
        # Get Agent B's public key
        public_b = IdentityCrypto.public_key_from_bytes(record_b.public_key_bytes)

        # Sign with A's key, verify with B's key — must fail
        message = b"impersonation attempt"
        signature = crypto.sign(message, private_a)
        assert crypto.verify(message, signature, public_b) is False

    def test_agents_have_different_dids(self, key_service: KeyService) -> None:
        """Each agent has a unique DID."""
        r_a = key_service.ensure_keypair("agent-a")
        r_b = key_service.ensure_keypair("agent-b")
        assert r_a.did != r_b.did


# ---------------------------------------------------------------------------
# Key Rotation Grace Period Boundaries
# ---------------------------------------------------------------------------


class TestRotationGracePeriod:
    def test_old_key_valid_during_grace_period(self, key_service: KeyService) -> None:
        """Old key is still active during grace period."""
        old = key_service.ensure_keypair("agent-1")
        key_service.rotate_key("agent-1", grace_period_hours=24)

        # Both keys should be active
        keys = key_service.get_active_keys("agent-1")
        key_ids = {k.key_id for k in keys}
        assert old.key_id in key_ids

    def test_old_key_invalid_after_grace_period(
        self, key_service: KeyService, session_factory: Any
    ) -> None:
        """Old key is filtered after grace period expires."""
        old = key_service.ensure_keypair("agent-1")
        key_service.rotate_key("agent-1", grace_period_hours=1)

        # Manually set old key's expiry to the past
        with session_factory() as session:
            session.execute(
                update(AgentKeyModel)
                .where(AgentKeyModel.key_id == old.key_id)
                .values(expires_at=_utcnow_naive() - timedelta(hours=1))
            )
            session.commit()

        keys = key_service.get_active_keys("agent-1")
        key_ids = {k.key_id for k in keys}
        assert old.key_id not in key_ids

    def test_new_key_always_valid(self, key_service: KeyService) -> None:
        """New key from rotation has no expiry."""
        key_service.ensure_keypair("agent-1")
        new = key_service.rotate_key("agent-1")
        assert new.expires_at is None


# ---------------------------------------------------------------------------
# DID Mismatch Detection
# ---------------------------------------------------------------------------


class TestDIDMismatch:
    def test_did_resolves_to_correct_key(self, key_service: KeyService) -> None:
        """DID stored in record must resolve to the stored public key."""
        record = key_service.ensure_keypair("agent-1")
        resolved_pk = resolve_did_key(record.did)
        resolved_bytes = IdentityCrypto.public_key_to_bytes(resolved_pk)
        assert resolved_bytes == record.public_key_bytes

    def test_tampered_did_resolves_to_wrong_key(self, key_service: KeyService) -> None:
        """A different DID resolves to a different public key."""
        record_a = key_service.ensure_keypair("agent-a")
        record_b = key_service.ensure_keypair("agent-b")

        resolved_a = resolve_did_key(record_a.did)
        resolved_a_bytes = IdentityCrypto.public_key_to_bytes(resolved_a)
        assert resolved_a_bytes != record_b.public_key_bytes


# ---------------------------------------------------------------------------
# Revocation Behavior
# ---------------------------------------------------------------------------


class TestRevocationSecurity:
    def test_revoked_key_immediately_unusable(self, key_service: KeyService) -> None:
        """Revoking a key makes it immediately unavailable."""
        record = key_service.ensure_keypair("agent-1")
        key_service.revoke_key(record.key_id)

        # Should not appear in active keys
        assert key_service.get_active_keys("agent-1") == []
        # Should be marked as revoked
        assert key_service.is_revoked(record.key_id) is True
        # Should not be decryptable
        with pytest.raises(ValueError, match="not active"):
            key_service.decrypt_private_key(record.key_id)

    def test_revocation_persists_across_cache(self, key_service: KeyService) -> None:
        """Revocation survives cache invalidation."""
        record = key_service.ensure_keypair("agent-1")
        key_service.revoke_key(record.key_id)

        # Force cache miss by checking is_revoked (which checks DB on miss)
        assert key_service.is_revoked(record.key_id) is True
