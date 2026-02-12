"""Tests for KeyService — idempotent key management (Issue #1355).

Covers:
- Idempotent ensure_keypair (create new, return existing)
- Active key queries with TTL cache
- Key rotation with grace period
- Key revocation
- Cache invalidation behavior
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.key_service import KeyService
from nexus.storage.models import Base


def _utcnow_naive() -> datetime:
    """UTC naive datetime for tests — matches key_service._utcnow_naive()."""
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Any:
    """In-memory SQLite for testing."""
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
    """Mock OAuthCrypto that round-trips encrypt/decrypt."""
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
    return KeyService(
        session_factory=session_factory,
        crypto=crypto,
        cache_ttl=1,  # Short TTL for test speed
    )


# ---------------------------------------------------------------------------
# ensure_keypair Tests
# ---------------------------------------------------------------------------


class TestEnsureKeypair:
    def test_creates_new_keypair(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        assert record.agent_id == "agent-1"
        assert record.algorithm == "Ed25519"
        assert record.is_active is True
        assert record.did.startswith("did:key:z")
        assert len(record.public_key_bytes) == 32
        assert record.expires_at is None
        assert record.revoked_at is None

    def test_idempotent(self, key_service: KeyService) -> None:
        """Calling ensure_keypair twice returns the same key."""
        record1 = key_service.ensure_keypair("agent-1")
        record2 = key_service.ensure_keypair("agent-1")
        assert record1.key_id == record2.key_id
        assert record1.did == record2.did

    def test_different_agents_get_different_keys(self, key_service: KeyService) -> None:
        r1 = key_service.ensure_keypair("agent-1")
        r2 = key_service.ensure_keypair("agent-2")
        assert r1.key_id != r2.key_id
        assert r1.did != r2.did

    def test_empty_agent_id_raises(self, key_service: KeyService) -> None:
        with pytest.raises(ValueError, match="agent_id is required"):
            key_service.ensure_keypair("")


# ---------------------------------------------------------------------------
# get_active_keys Tests
# ---------------------------------------------------------------------------


class TestGetActiveKeys:
    def test_returns_active_keys(self, key_service: KeyService) -> None:
        key_service.ensure_keypair("agent-1")
        keys = key_service.get_active_keys("agent-1")
        assert len(keys) == 1
        assert keys[0].is_active is True

    def test_returns_empty_for_unknown_agent(self, key_service: KeyService) -> None:
        keys = key_service.get_active_keys("nonexistent")
        assert keys == []

    def test_newest_first(self, key_service: KeyService) -> None:
        """After rotation, newest key should be first."""
        key_service.ensure_keypair("agent-1")
        key_service.rotate_key("agent-1", grace_period_hours=24)
        keys = key_service.get_active_keys("agent-1")
        assert len(keys) == 2
        # Newest first
        assert keys[0].created_at >= keys[1].created_at


# ---------------------------------------------------------------------------
# get_public_key Tests
# ---------------------------------------------------------------------------


class TestGetPublicKey:
    def test_found(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        found = key_service.get_public_key(record.key_id)
        assert found is not None
        assert found.key_id == record.key_id

    def test_not_found(self, key_service: KeyService) -> None:
        assert key_service.get_public_key("nonexistent-id") is None

    def test_get_public_key_object(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        pk = key_service.get_public_key_object(record.key_id)
        assert pk is not None
        # Verify the object matches the stored bytes
        raw = IdentityCrypto.public_key_to_bytes(pk)
        assert raw == record.public_key_bytes


# ---------------------------------------------------------------------------
# rotate_key Tests
# ---------------------------------------------------------------------------


class TestRotateKey:
    def test_creates_new_key(self, key_service: KeyService) -> None:
        old = key_service.ensure_keypair("agent-1")
        new = key_service.rotate_key("agent-1", grace_period_hours=24)
        assert new.key_id != old.key_id
        assert new.did != old.did
        assert new.is_active is True
        assert new.expires_at is None  # New key has no expiry

    def test_old_key_gets_expiry(self, key_service: KeyService) -> None:
        key_service.ensure_keypair("agent-1")
        key_service.rotate_key("agent-1", grace_period_hours=24)
        keys = key_service.get_active_keys("agent-1")

        # Old key (second in list) should have expiry set
        old_key = keys[1]
        assert old_key.expires_at is not None
        assert old_key.expires_at > _utcnow_naive()

    def test_grace_period_duration(self, key_service: KeyService) -> None:
        key_service.ensure_keypair("agent-1")
        key_service.rotate_key("agent-1", grace_period_hours=48)
        keys = key_service.get_active_keys("agent-1")
        old_key = keys[1]
        assert old_key.expires_at is not None
        # Should be ~48 hours from now (with some tolerance)
        delta = old_key.expires_at - _utcnow_naive()
        assert timedelta(hours=47) < delta < timedelta(hours=49)


# ---------------------------------------------------------------------------
# revoke_key Tests
# ---------------------------------------------------------------------------


class TestRevokeKey:
    def test_revoke_existing_key(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        assert key_service.revoke_key(record.key_id) is True

    def test_revoke_nonexistent_returns_false(self, key_service: KeyService) -> None:
        assert key_service.revoke_key("nonexistent-id") is False

    def test_revoked_key_not_in_active_keys(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        key_service.revoke_key(record.key_id)
        keys = key_service.get_active_keys("agent-1")
        assert len(keys) == 0

    def test_is_revoked(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        assert key_service.is_revoked(record.key_id) is False
        key_service.revoke_key(record.key_id)
        assert key_service.is_revoked(record.key_id) is True

    def test_revoked_key_returns_none_from_get_public_key_object(
        self, key_service: KeyService
    ) -> None:
        record = key_service.ensure_keypair("agent-1")
        key_service.revoke_key(record.key_id)
        assert key_service.get_public_key_object(record.key_id) is None


# ---------------------------------------------------------------------------
# decrypt_private_key Tests
# ---------------------------------------------------------------------------


class TestDecryptPrivateKey:
    def test_decrypt_active_key(self, key_service: KeyService, crypto: IdentityCrypto) -> None:
        record = key_service.ensure_keypair("agent-1")
        private_key = key_service.decrypt_private_key(record.key_id)

        # Verify the decrypted key can sign
        message = b"test message"
        signature = crypto.sign(message, private_key)
        public = IdentityCrypto.public_key_from_bytes(record.public_key_bytes)
        assert crypto.verify(message, signature, public) is True

    def test_decrypt_nonexistent_raises(self, key_service: KeyService) -> None:
        with pytest.raises(ValueError, match="not found"):
            key_service.decrypt_private_key("nonexistent-id")

    def test_decrypt_revoked_raises(self, key_service: KeyService) -> None:
        record = key_service.ensure_keypair("agent-1")
        key_service.revoke_key(record.key_id)
        with pytest.raises(ValueError, match="not active"):
            key_service.decrypt_private_key(record.key_id)
