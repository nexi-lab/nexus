"""Unit tests for IPC message signing and verification (#1729)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import create_did_key
from nexus.identity.key_service import AgentKeyRecord
from nexus.ipc.envelope import MessageEnvelope, MessageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    sender: str = "agent:alice",
    recipient: str = "agent:bob",
    msg_id: str = "msg_sign_001",
    payload: dict | None = None,
    timestamp: datetime | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        type=MessageType.TASK,
        id=msg_id,
        timestamp=timestamp or datetime(2026, 2, 15, 12, 0, 0, tzinfo=UTC),
        payload=payload or {"action": "review"},
    )


class FakeTokenEncryptor:
    """Trivial encryptor that just prefixes 'enc:' for testing."""

    def encrypt_token(self, token: str) -> str:
        return f"enc:{token}"

    def decrypt_token(self, encrypted: str) -> str:
        return encrypted.removeprefix("enc:")


def _make_crypto() -> IdentityCrypto:
    return IdentityCrypto(FakeTokenEncryptor())


def _make_key_record(
    agent_id: str = "agent:alice",
    *,
    crypto: IdentityCrypto | None = None,
    is_active: bool = True,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> tuple[AgentKeyRecord, Ed25519PrivateKey]:
    """Create a key record + private key for testing."""
    crypto = crypto or _make_crypto()
    private_key, public_key = crypto.generate_keypair()
    pub_bytes = IdentityCrypto.public_key_to_bytes(public_key)
    did = create_did_key(public_key)
    key_id = "test-key-001"
    return (
        AgentKeyRecord(
            key_id=key_id,
            agent_id=agent_id,
            algorithm="Ed25519",
            public_key_bytes=pub_bytes,
            did=did,
            is_active=is_active,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=expires_at,
            revoked_at=revoked_at,
        ),
        private_key,
    )


# ===========================================================================
# Phase 1: Canonical serialization tests
# ===========================================================================


class TestSigningBytes:
    """Tests for MessageEnvelope.signing_bytes() canonical serialization."""

    def test_signing_bytes_deterministic(self) -> None:
        """Same envelope produces identical bytes on repeated calls."""
        env = _make_envelope()
        assert env.signing_bytes() == env.signing_bytes()

    def test_signing_bytes_excludes_signature_fields(self) -> None:
        """signature, signer_did, signer_key_id must NOT appear in signing bytes."""
        env = _make_envelope()
        # Create a signed envelope (model is frozen, use model_copy)
        signed = env.model_copy(
            update={
                "signature": "fakesig",
                "signer_did": "did:key:z123",
                "signer_key_id": "key-001",
            }
        )
        payload = json.loads(signed.signing_bytes())
        assert "signature" not in payload
        assert "signer_did" not in payload
        assert "signer_key_id" not in payload

    def test_signing_bytes_excludes_routing(self) -> None:
        """routing metadata must NOT appear in signing bytes."""
        env = _make_envelope()
        payload = json.loads(env.signing_bytes())
        assert "routing" not in payload

    def test_signing_bytes_includes_all_core_fields(self) -> None:
        """from, to, type, payload, timestamp, id, nexus_message all present."""
        env = _make_envelope()
        payload = json.loads(env.signing_bytes())
        assert payload["from"] == "agent:alice"
        assert payload["to"] == "agent:bob"
        assert payload["type"] == "task"
        assert payload["payload"] == {"action": "review"}
        assert "id" in payload
        assert "nexus_message" in payload
        assert "timestamp" in payload

    def test_signing_bytes_sorted_keys(self) -> None:
        """Output must use sorted keys for determinism."""
        env = _make_envelope()
        raw = env.signing_bytes().decode()
        keys = list(json.loads(raw).keys())
        assert keys == sorted(keys)

    def test_signing_bytes_compact_separators(self) -> None:
        """Output uses compact JSON separators (no padding around : and ,)."""
        env = _make_envelope()
        raw = env.signing_bytes().decode()
        # Compact separators: no ": " or ", " (spaces may appear in values like timestamps)
        assert '": ' not in raw
        assert '", ' not in raw

    def test_envelope_with_signature_fields_roundtrip(self) -> None:
        """Serialize/deserialize with optional signature fields."""
        env = _make_envelope()
        signed = env.model_copy(
            update={
                "signature": "c2lnbmF0dXJl",
                "signer_did": "did:key:z6Mk123",
                "signer_key_id": "key-uuid-001",
            }
        )
        data = signed.to_bytes()
        restored = MessageEnvelope.from_bytes(data)
        assert restored.signature == "c2lnbmF0dXJl"
        assert restored.signer_did == "did:key:z6Mk123"
        assert restored.signer_key_id == "key-uuid-001"

    def test_unsigned_envelope_has_none_signature_fields(self) -> None:
        """Default envelope has None for all signature fields."""
        env = _make_envelope()
        assert env.signature is None
        assert env.signer_did is None
        assert env.signer_key_id is None

    def test_signing_bytes_different_payloads_produce_different_bytes(self) -> None:
        """Different payloads must produce different signing bytes."""
        env1 = _make_envelope(payload={"a": 1})
        env2 = _make_envelope(payload={"a": 2})
        assert env1.signing_bytes() != env2.signing_bytes()


# ===========================================================================
# Phase 2: MessageSigner + MessageVerifier tests
# ===========================================================================


class TestMessageSigner:
    """Tests for MessageSigner sign() operation."""

    def test_sign_envelope_adds_signature_fields(self) -> None:
        from nexus.ipc.signing import MessageSigner

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        env = _make_envelope()
        signed = signer.sign(env)

        assert signed.signature is not None
        assert signed.signer_did == record.did
        assert signed.signer_key_id == record.key_id
        # Original untouched (immutable)
        assert env.signature is None

    def test_sign_verify_roundtrip(self) -> None:
        from nexus.ipc.signing import MessageSigner, MessageVerifier

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key
        key_service.get_public_key.return_value = record

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        env = _make_envelope()
        signed = signer.sign(env)
        result = verifier.verify(signed)

        assert result.valid is True

    def test_verify_tampered_payload_fails(self) -> None:
        from nexus.ipc.signing import MessageSigner, MessageVerifier

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key
        key_service.get_public_key.return_value = record

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        env = _make_envelope()
        signed = signer.sign(env)
        # Tamper with payload
        tampered = signed.model_copy(update={"payload": {"action": "tampered"}})
        result = verifier.verify(tampered)

        assert result.valid is False
        assert "signature" in result.detail.lower() or "invalid" in result.detail.lower()

    def test_verify_tampered_sender_fails(self) -> None:
        from nexus.ipc.signing import MessageSigner, MessageVerifier

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key
        key_service.get_public_key.return_value = record

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        env = _make_envelope()
        signed = signer.sign(env)
        tampered = signed.model_copy(update={"sender": "agent:mallory"})
        result = verifier.verify(tampered)

        assert result.valid is False

    def test_verify_unknown_key_id_fails(self) -> None:
        from nexus.ipc.signing import MessageSigner, MessageVerifier

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key
        # First call for signing succeeds, lookup for verify returns None
        key_service.get_public_key.return_value = None

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        signed = signer.sign(_make_envelope())
        result = verifier.verify(signed)

        assert result.valid is False
        assert "not found" in result.detail.lower()

    def test_verify_revoked_key_fails(self) -> None:
        from nexus.ipc.signing import MessageSigner, MessageVerifier

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)
        revoked_record = AgentKeyRecord(
            key_id=record.key_id,
            agent_id=record.agent_id,
            algorithm=record.algorithm,
            public_key_bytes=record.public_key_bytes,
            did=record.did,
            is_active=False,
            created_at=record.created_at,
            expires_at=record.expires_at,
            revoked_at=datetime(2026, 2, 14, tzinfo=UTC),
        )

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key
        key_service.get_public_key.return_value = revoked_record

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        signed = signer.sign(_make_envelope())
        result = verifier.verify(signed)

        assert result.valid is False
        assert "revoked" in result.detail.lower() or "not active" in result.detail.lower()

    def test_auto_provision_keypair(self) -> None:
        """Signer calls ensure_keypair on first sign."""
        from nexus.ipc.signing import MessageSigner

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        signer.sign(_make_envelope())

        key_service.ensure_keypair.assert_called_once_with("agent:alice")

    def test_private_key_cached_after_first_sign(self) -> None:
        """Private key is decrypted once and cached for subsequent signs."""
        from nexus.ipc.signing import MessageSigner

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        signer.sign(_make_envelope())
        signer.sign(_make_envelope(msg_id="msg_sign_002"))

        # ensure_keypair and decrypt_private_key called only once
        key_service.ensure_keypair.assert_called_once()
        key_service.decrypt_private_key.assert_called_once()

    def test_verify_unsigned_envelope(self) -> None:
        """Verifying an unsigned envelope returns invalid."""
        from nexus.ipc.signing import MessageVerifier

        crypto = _make_crypto()
        key_service = MagicMock()
        verifier = MessageVerifier(key_service, crypto)

        env = _make_envelope()
        result = verifier.verify(env)

        assert result.valid is False
        assert "unsigned" in result.detail.lower() or "no signature" in result.detail.lower()

    def test_signing_performance_under_1ms(self) -> None:
        """Average signing time must be under 1ms (1000 iterations)."""
        from nexus.ipc.signing import MessageSigner

        crypto = _make_crypto()
        record, private_key = _make_key_record(crypto=crypto)

        key_service = MagicMock()
        key_service.ensure_keypair.return_value = record
        key_service.decrypt_private_key.return_value = private_key

        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")

        iterations = 1000
        envelopes = [_make_envelope(msg_id=f"msg_perf_{i}") for i in range(iterations)]

        # Warm up (provision key)
        signer.sign(envelopes[0])

        start = time.perf_counter()
        for env in envelopes:
            signer.sign(env)
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        assert avg_ms < 1.0, f"Average signing time {avg_ms:.3f}ms exceeds 1ms budget"


class TestSigningMode:
    """Tests for the SigningMode enum."""

    def test_signing_mode_values(self) -> None:
        from nexus.ipc.signing import SigningMode

        assert SigningMode.OFF == "off"
        assert SigningMode.VERIFY_ONLY == "verify_only"
        assert SigningMode.ENFORCE == "enforce"

    def test_signing_mode_is_str_enum(self) -> None:
        from nexus.ipc.signing import SigningMode

        assert isinstance(SigningMode.OFF, str)


class TestVerifyResult:
    """Tests for VerifyResult dataclass."""

    def test_verify_result_frozen(self) -> None:
        from nexus.ipc.signing import VerifyResult

        result = VerifyResult(valid=True, detail="ok")
        with pytest.raises(AttributeError):
            result.valid = False  # type: ignore[misc]

    def test_verify_result_default_detail(self) -> None:
        from nexus.ipc.signing import VerifyResult

        result = VerifyResult(valid=True)
        assert result.detail == ""
