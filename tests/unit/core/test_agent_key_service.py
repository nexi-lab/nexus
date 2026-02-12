"""Unit tests for AgentKeyService (KYA Phase 1, Issue #1355).

TDD tests covering:
1. Key generation: valid keypair, JWK format, thumbprint, Ed25519 algorithm
2. Key persistence: store/retrieve roundtrip, Fernet encryption, decrypt+verify
3. Key rotation: new key generated, old key stays active, list shows both
4. Key revocation: marks revoked_at, revoked key filtered, cache invalidation
5. JWK format: RFC 7517 compliance, RFC 7638 thumbprint, canonical JSON
6. Edge cases: expired key, agent with no keys, non-existent key
7. Atomicity: key gen in same session as registration
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_key_record import AgentIdentityInfo, AgentKeyRecord
from nexus.core.agent_key_service import (
    AgentKeyService,
    _base64url_encode,
    _compute_jwk_thumbprint,
    _ed25519_to_jwk,
)
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.storage.models import Base
from nexus.storage.models.agent_key import AgentKeyModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite database (thread-safe)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Session factory for tests."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def crypto():
    """OAuthCrypto instance with a random key for testing."""
    return OAuthCrypto()


@pytest.fixture
def key_service(session_factory, crypto):
    """AgentKeyService instance for testing."""
    return AgentKeyService(
        session_factory=session_factory,
        crypto=crypto,
        cache_maxsize=100,
        cache_ttl=300,
    )


# ---------------------------------------------------------------------------
# 1. Key generation tests
# ---------------------------------------------------------------------------


class TestKeyGeneration:
    """Tests for Ed25519 key pair generation."""

    def test_generate_key_pair_returns_record(self, key_service, session_factory):
        """generate_key_pair returns a valid AgentKeyRecord."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", "default", session)
            session.commit()

        assert isinstance(record, AgentKeyRecord)
        assert record.agent_id == "agent-1"
        assert record.zone_id == "default"
        assert record.algorithm == "Ed25519"
        assert record.has_private_key is True
        assert record.revoked_at is None
        assert record.expires_at is None

    def test_generate_key_pair_produces_valid_jwk(self, key_service, session_factory):
        """Generated JWK has correct kty, crv, and x fields."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", "default", session)
            session.commit()

        jwk = record.public_key_jwk
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert "x" in jwk
        # x must be base64url without padding
        assert "=" not in jwk["x"]
        assert "+" not in jwk["x"]
        assert "/" not in jwk["x"]

    def test_key_id_is_jwk_thumbprint(self, key_service, session_factory):
        """key_id is the SHA-256 JWK Thumbprint (RFC 7638)."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        expected_thumbprint = _compute_jwk_thumbprint(record.public_key_jwk)
        assert record.key_id == expected_thumbprint

    def test_thumbprint_is_deterministic(self, key_service, session_factory):
        """Same public key always produces the same thumbprint."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        t1 = _compute_jwk_thumbprint(record.public_key_jwk)
        t2 = _compute_jwk_thumbprint(record.public_key_jwk)
        assert t1 == t2

    def test_zone_id_propagated(self, key_service, session_factory):
        """zone_id is stored with the key."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", "zone-42", session)
            session.commit()

        assert record.zone_id == "zone-42"

    def test_generated_key_is_valid_ed25519(self, key_service, session_factory):
        """The stored public key can be loaded as a valid Ed25519 public key."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        x_bytes = base64.urlsafe_b64decode(record.public_key_jwk["x"] + "==")
        public_key = Ed25519PublicKey.from_public_bytes(x_bytes)
        raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert len(raw) == 32

    def test_each_generation_produces_unique_key(self, key_service, session_factory):
        """Two calls generate different key pairs."""
        with session_factory() as session:
            r1 = key_service.generate_key_pair("agent-1", None, session)
            r2 = key_service.generate_key_pair("agent-2", None, session)
            session.commit()

        assert r1.key_id != r2.key_id
        assert r1.public_key_jwk["x"] != r2.public_key_jwk["x"]


# ---------------------------------------------------------------------------
# 2. Key persistence tests
# ---------------------------------------------------------------------------


class TestKeyPersistence:
    """Tests for key storage and retrieval."""

    def test_store_and_retrieve_roundtrip(self, key_service, session_factory):
        """Key can be stored and retrieved from the database."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", "default", session)
            session.commit()

        retrieved = key_service.get_public_key("agent-1")
        assert retrieved is not None
        assert retrieved.key_id == record.key_id
        assert retrieved.public_key_jwk == record.public_key_jwk
        assert retrieved.agent_id == "agent-1"

    def test_private_key_is_fernet_encrypted(self, key_service, session_factory, crypto):
        """Private key is stored encrypted, not plaintext."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Read raw model from DB
        with session_factory() as session:
            model = session.query(AgentKeyModel).first()
            encrypted = model.encrypted_private_key

        assert encrypted is not None
        # Should be Fernet-encrypted (base64 string, starts with gAAAAA)
        assert encrypted.startswith("gAAAAA")
        # Should be decryptable
        decrypted = crypto.decrypt_token(encrypted)
        private_bytes = base64.b64decode(decrypted)
        assert len(private_bytes) == 32  # Ed25519 private key is 32 bytes

    def test_get_public_key_not_found_returns_none(self, key_service):
        """get_public_key for non-existent agent returns None."""
        assert key_service.get_public_key("nonexistent") is None

    def test_get_by_key_id(self, key_service, session_factory):
        """get_public_key_by_key_id returns the correct key."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        retrieved = key_service.get_public_key_by_key_id(record.key_id)
        assert retrieved is not None
        assert retrieved.key_id == record.key_id
        assert retrieved.agent_id == "agent-1"

    def test_get_by_key_id_not_found(self, key_service):
        """get_public_key_by_key_id returns None for non-existent key."""
        assert key_service.get_public_key_by_key_id("nonexistent") is None

    def test_decrypt_and_verify_signature(self, key_service, session_factory, crypto):
        """Decrypted private key can produce a valid signature."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Get encrypted private key from DB
        with session_factory() as session:
            model = session.query(AgentKeyModel).first()
            decrypted_b64 = crypto.decrypt_token(model.encrypted_private_key)

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_bytes = base64.b64decode(decrypted_b64)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)

        # Sign a message
        message = b"test message for verification"
        signature = private_key.sign(message)

        # Verify with the public key from JWK
        x_bytes = base64.urlsafe_b64decode(record.public_key_jwk["x"] + "==")
        public_key = Ed25519PublicKey.from_public_bytes(x_bytes)
        public_key.verify(signature, message)  # Raises if invalid


# ---------------------------------------------------------------------------
# 3. Key rotation tests
# ---------------------------------------------------------------------------


class TestKeyRotation:
    """Tests for key rotation (new key + old key coexistence)."""

    def test_rotate_generates_new_key(self, key_service, session_factory):
        """Rotating generates a new key with a different key_id."""
        with session_factory() as session:
            old_record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        with session_factory() as session:
            new_record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        assert old_record.key_id != new_record.key_id

    def test_old_key_not_revoked_on_rotation(self, key_service, session_factory):
        """Old key is NOT automatically revoked on rotation (grace period)."""
        with session_factory() as session:
            old_record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Old key should still be retrievable
        old_key = key_service.get_public_key_by_key_id(old_record.key_id)
        assert old_key is not None
        assert old_key.revoked_at is None

    def test_list_shows_both_keys(self, key_service, session_factory):
        """list_keys returns both old and new keys."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        keys = key_service.list_keys("agent-1")
        assert len(keys) == 2

    def test_get_public_key_returns_newest(self, key_service, session_factory):
        """get_public_key returns the most recently created active key."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Clear cache to force DB lookup
        with key_service._cache_lock:
            key_service._key_cache.clear()

        with session_factory() as session:
            new_record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Clear cache again
        with key_service._cache_lock:
            key_service._key_cache.clear()

        retrieved = key_service.get_public_key("agent-1")
        assert retrieved is not None
        assert retrieved.key_id == new_record.key_id


# ---------------------------------------------------------------------------
# 4. Key revocation tests
# ---------------------------------------------------------------------------


class TestKeyRevocation:
    """Tests for key revocation."""

    def test_revoke_marks_revoked_at(self, key_service, session_factory):
        """Revoking a key sets revoked_at timestamp."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        result = key_service.revoke_key("agent-1", record.key_id)
        assert result is True

        # Verify revoked_at is set
        revoked = key_service.get_public_key_by_key_id(record.key_id)
        assert revoked is not None
        assert revoked.revoked_at is not None

    def test_revoked_key_not_returned_by_get_public_key(self, key_service, session_factory):
        """get_public_key skips revoked keys."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        key_service.revoke_key("agent-1", record.key_id)

        # Clear cache
        with key_service._cache_lock:
            key_service._key_cache.clear()

        assert key_service.get_public_key("agent-1") is None

    def test_revoke_nonexistent_key_returns_false(self, key_service):
        """Revoking a non-existent key returns False."""
        assert key_service.revoke_key("agent-1", "nonexistent") is False

    def test_revoke_already_revoked_returns_false(self, key_service, session_factory):
        """Revoking an already-revoked key returns False."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        assert key_service.revoke_key("agent-1", record.key_id) is True
        assert key_service.revoke_key("agent-1", record.key_id) is False

    def test_cache_invalidated_on_revoke(self, key_service, session_factory):
        """Revoking a key removes it from the cache."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Warm cache
        key_service.get_public_key("agent-1")
        with key_service._cache_lock:
            assert "agent-1" in key_service._key_cache

        key_service.revoke_key("agent-1", record.key_id)

        with key_service._cache_lock:
            assert "agent-1" not in key_service._key_cache

    def test_list_keys_excludes_revoked_by_default(self, key_service, session_factory):
        """list_keys(include_revoked=False) excludes revoked keys."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        key_service.revoke_key("agent-1", record.key_id)
        keys = key_service.list_keys("agent-1", include_revoked=False)
        assert len(keys) == 0

    def test_list_keys_includes_revoked_when_requested(self, key_service, session_factory):
        """list_keys(include_revoked=True) includes revoked keys."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        key_service.revoke_key("agent-1", record.key_id)
        keys = key_service.list_keys("agent-1", include_revoked=True)
        assert len(keys) == 1
        assert keys[0].revoked_at is not None


# ---------------------------------------------------------------------------
# 5. JWK format tests
# ---------------------------------------------------------------------------


class TestJWKFormat:
    """Tests for RFC 7517 / RFC 7638 compliance."""

    def test_rfc7517_okp_fields(self):
        """JWK contains required OKP fields."""
        dummy_bytes = b"\x00" * 32
        jwk = _ed25519_to_jwk(dummy_bytes)
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert "x" in jwk

    def test_rfc7638_thumbprint_canonical_json(self):
        """Thumbprint uses canonical JSON per RFC 7638."""
        jwk = {"kty": "OKP", "crv": "Ed25519", "x": "test_value"}
        expected_json = '{"crv":"Ed25519","kty":"OKP","x":"test_value"}'
        expected_hash = hashlib.sha256(expected_json.encode("ascii")).digest()
        expected_thumbprint = _base64url_encode(expected_hash)

        assert _compute_jwk_thumbprint(jwk) == expected_thumbprint

    def test_base64url_no_padding(self):
        """base64url encoding has no padding characters."""
        data = b"\xff\xff\xff"
        encoded = _base64url_encode(data)
        assert "=" not in encoded

    def test_base64url_url_safe_chars(self):
        """base64url uses URL-safe characters only."""
        data = b"\xff\xff\xff\xfe\xfd"
        encoded = _base64url_encode(data)
        assert "+" not in encoded
        assert "/" not in encoded


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    def test_agent_with_no_keys(self, key_service):
        """Agent with no keys returns None from get_public_key."""
        assert key_service.get_public_key("no-keys-agent") is None

    def test_list_keys_empty_agent(self, key_service):
        """list_keys for agent with no keys returns empty list."""
        assert key_service.list_keys("no-keys-agent") == []

    def test_delete_agent_keys(self, key_service, session_factory):
        """delete_agent_keys removes all keys for an agent."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        with session_factory() as session:
            deleted = key_service.delete_agent_keys("agent-1", session)
            session.commit()

        assert deleted == 2
        assert key_service.list_keys("agent-1") == []

    def test_delete_agent_keys_invalidates_cache(self, key_service, session_factory):
        """delete_agent_keys clears the agent's cache entry."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        # Warm cache
        key_service.get_public_key("agent-1")

        with session_factory() as session:
            key_service.delete_agent_keys("agent-1", session)
            session.commit()

        with key_service._cache_lock:
            assert "agent-1" not in key_service._key_cache


# ---------------------------------------------------------------------------
# 7. Identity verification tests
# ---------------------------------------------------------------------------


class TestIdentityVerification:
    """Tests for verify_identity."""

    def test_verify_identity_returns_info(self, key_service, session_factory):
        """verify_identity returns AgentIdentityInfo with correct data."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", "zone-1", session)
            session.commit()

        info = key_service.verify_identity("agent-1", "alice", "zone-1")
        assert isinstance(info, AgentIdentityInfo)
        assert info.agent_id == "agent-1"
        assert info.owner_id == "alice"
        assert info.zone_id == "zone-1"
        assert info.key_id == record.key_id
        assert info.algorithm == "Ed25519"
        assert info.public_key_jwk == record.public_key_jwk

    def test_verify_identity_no_key_returns_none(self, key_service):
        """verify_identity returns None if agent has no active key."""
        info = key_service.verify_identity("no-key-agent", "alice", None)
        assert info is None

    def test_verify_identity_revoked_key_returns_none(self, key_service, session_factory):
        """verify_identity returns None if only key is revoked."""
        with session_factory() as session:
            record = key_service.generate_key_pair("agent-1", None, session)
            session.commit()

        key_service.revoke_key("agent-1", record.key_id)

        # Clear cache
        with key_service._cache_lock:
            key_service._key_cache.clear()

        info = key_service.verify_identity("agent-1", "alice", None)
        assert info is None


# ---------------------------------------------------------------------------
# 8. Atomicity tests (key gen in same session as registration)
# ---------------------------------------------------------------------------


class TestAtomicity:
    """Tests for transactional atomicity."""

    def test_key_gen_uses_external_session(self, key_service, session_factory):
        """Key generation uses the provided session, not an internal one."""
        with session_factory() as session:
            key_service.generate_key_pair("agent-1", None, session)
            # Don't commit — rollback
            session.rollback()

        # Key should NOT be persisted
        assert key_service.get_public_key("agent-1") is None

    def test_key_gen_and_agent_in_same_transaction(self, key_service, session_factory):
        """Key gen and agent registration commit together."""
        from nexus.storage.models import AgentRecordModel

        with session_factory() as session:
            # Simulate agent registration
            agent_model = AgentRecordModel(
                agent_id="agent-tx",
                owner_id="alice",
                state="UNKNOWN",
                generation=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(agent_model)
            session.flush()

            # Generate key in same session
            record = key_service.generate_key_pair("agent-tx", None, session)
            session.commit()

        # Both should be persisted
        retrieved = key_service.get_public_key("agent-tx")
        assert retrieved is not None
        assert retrieved.key_id == record.key_id

    def test_rollback_removes_both_agent_and_key(self, key_service, session_factory):
        """Rollback removes both agent and key (atomic failure)."""
        from nexus.storage.models import AgentRecordModel

        with session_factory() as session:
            agent_model = AgentRecordModel(
                agent_id="agent-fail",
                owner_id="alice",
                state="UNKNOWN",
                generation=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(agent_model)
            session.flush()
            key_service.generate_key_pair("agent-fail", None, session)
            session.rollback()

        assert key_service.get_public_key("agent-fail") is None

        # Verify agent also not persisted
        with session_factory() as session:
            from nexus.storage.models import AgentRecordModel

            agent = session.query(AgentRecordModel).filter_by(agent_id="agent-fail").first()
            assert agent is None

    def test_fernet_encryption_failure_propagates(self, session_factory):
        """If Fernet encryption fails, no key is persisted."""
        bad_crypto = MagicMock()
        bad_crypto.encrypt_token.side_effect = ValueError("encryption failed")

        service = AgentKeyService(
            session_factory=session_factory,
            crypto=bad_crypto,
        )

        with pytest.raises(ValueError, match="encryption failed"), session_factory() as session:
            service.generate_key_pair("agent-1", None, session)
            session.commit()

        assert service.get_public_key("agent-1") is None
