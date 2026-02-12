"""Integration test: Golden-path agent registration + identity provisioning (Issue #1355, Decision #12B).

Tests the full flow:
1. Register agent → auto-provisioned Ed25519 keypair + DID
2. Query active keys → key returned
3. Verify signature → round-trip sign/verify with provisioned key
4. Rotate key → old key has grace period, new key active
5. Revoke old key → immediately unusable
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import resolve_did_key
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
    """In-memory SQLite for integration testing."""
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
    return KeyService(session_factory=session_factory, crypto=crypto, cache_ttl=0)


# ---------------------------------------------------------------------------
# Golden-Path Integration Test
# ---------------------------------------------------------------------------


class TestRegistrationGoldenPath:
    """Full registration + identity lifecycle."""

    def test_full_lifecycle(self, key_service: KeyService, crypto: IdentityCrypto) -> None:
        """Step 1-5: register → keys → sign/verify → rotate → revoke."""
        agent_id = "integration-agent-1"

        # Step 1: Provision identity (simulates register_agent calling ensure_keypair)
        record = key_service.ensure_keypair(agent_id)
        assert record.agent_id == agent_id
        assert record.did.startswith("did:key:z")
        assert record.algorithm == "Ed25519"
        assert record.is_active is True
        assert len(record.public_key_bytes) == 32

        # Step 2: Query active keys
        keys = key_service.get_active_keys(agent_id)
        assert len(keys) == 1
        assert keys[0].key_id == record.key_id

        # Step 3: Sign and verify a message
        private_key = key_service.decrypt_private_key(record.key_id)
        message = b"Hello from integration-agent-1"
        signature = crypto.sign(message, private_key)

        public_key = key_service.get_public_key_object(record.key_id)
        assert public_key is not None
        assert crypto.verify(message, signature, public_key) is True

        # Verify DID resolves to the correct public key
        resolved_pk = resolve_did_key(record.did)
        resolved_bytes = IdentityCrypto.public_key_to_bytes(resolved_pk)
        assert resolved_bytes == record.public_key_bytes

        # Step 4: Rotate key
        new_record = key_service.rotate_key(agent_id, grace_period_hours=24)
        assert new_record.key_id != record.key_id
        assert new_record.did != record.did
        assert new_record.is_active is True
        assert new_record.expires_at is None

        # Both keys should be active during grace period
        all_keys = key_service.get_active_keys(agent_id)
        assert len(all_keys) == 2
        key_ids = {k.key_id for k in all_keys}
        assert record.key_id in key_ids
        assert new_record.key_id in key_ids

        # Old key should have expiry set
        old_key = next(k for k in all_keys if k.key_id == record.key_id)
        assert old_key.expires_at is not None
        assert old_key.expires_at > _utcnow_naive()

        # New key should be able to sign
        new_private = key_service.decrypt_private_key(new_record.key_id)
        new_sig = crypto.sign(message, new_private)
        new_pub = key_service.get_public_key_object(new_record.key_id)
        assert new_pub is not None
        assert crypto.verify(message, new_sig, new_pub) is True

        # Step 5: Revoke old key
        assert key_service.revoke_key(record.key_id) is True
        assert key_service.is_revoked(record.key_id) is True

        # Old key should not be in active keys
        active = key_service.get_active_keys(agent_id)
        assert len(active) == 1
        assert active[0].key_id == new_record.key_id

        # Old key should not be usable for decryption
        with pytest.raises(ValueError, match="not active"):
            key_service.decrypt_private_key(record.key_id)

        # New key still works
        assert key_service.get_public_key_object(new_record.key_id) is not None

    def test_idempotent_registration(self, key_service: KeyService) -> None:
        """Calling ensure_keypair N times returns the same key."""
        agent_id = "idem-agent"
        r1 = key_service.ensure_keypair(agent_id)
        r2 = key_service.ensure_keypair(agent_id)
        r3 = key_service.ensure_keypair(agent_id)
        assert r1.key_id == r2.key_id == r3.key_id
        assert r1.did == r2.did == r3.did

    def test_multi_agent_isolation(self, key_service: KeyService, crypto: IdentityCrypto) -> None:
        """Different agents have independent identities — no cross-signing."""
        r_alice = key_service.ensure_keypair("alice")
        r_bob = key_service.ensure_keypair("bob")

        assert r_alice.key_id != r_bob.key_id
        assert r_alice.did != r_bob.did

        # Alice signs, Bob cannot verify
        alice_priv = key_service.decrypt_private_key(r_alice.key_id)
        msg = b"private message"
        sig = crypto.sign(msg, alice_priv)

        bob_pub = key_service.get_public_key_object(r_bob.key_id)
        assert bob_pub is not None
        assert crypto.verify(msg, sig, bob_pub) is False

        # Alice can verify her own signature
        alice_pub = key_service.get_public_key_object(r_alice.key_id)
        assert alice_pub is not None
        assert crypto.verify(msg, sig, alice_pub) is True

    def test_multiple_rotations(self, key_service: KeyService) -> None:
        """Multiple rotations maintain correct active key ordering."""
        agent_id = "rotating-agent"
        key_service.ensure_keypair(agent_id)

        # Rotate 3 times
        key_service.rotate_key(agent_id, grace_period_hours=24)
        key_service.rotate_key(agent_id, grace_period_hours=24)
        r4 = key_service.rotate_key(agent_id, grace_period_hours=24)

        keys = key_service.get_active_keys(agent_id)
        # All 4 keys should be active during grace period
        assert len(keys) == 4

        # Newest first
        assert keys[0].key_id == r4.key_id
        assert keys[0].expires_at is None  # Only the newest has no expiry
