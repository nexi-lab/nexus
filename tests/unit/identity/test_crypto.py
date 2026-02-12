"""Tests for identity crypto operations (Issue #1355).

Includes:
- Traditional unit tests for keypair gen, sign/verify, encrypt/decrypt
- Hypothesis property-based tests for roundtrip invariants (Decision #9B)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.identity.crypto import IdentityCrypto

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def crypto_no_fernet() -> IdentityCrypto:
    """IdentityCrypto without Fernet (for sign/verify only)."""
    return IdentityCrypto()


@pytest.fixture
def mock_oauth_crypto() -> MagicMock:
    """Mock OAuthCrypto that round-trips encrypt/decrypt."""
    mock = MagicMock()
    _store: dict[str, str] = {}

    def encrypt_side_effect(token: str) -> str:
        encrypted = f"ENCRYPTED:{token}"
        _store[encrypted] = token
        return encrypted

    def decrypt_side_effect(encrypted: str) -> str:
        if encrypted not in _store:
            raise ValueError(f"Cannot decrypt: {encrypted}")
        return _store[encrypted]

    mock.encrypt_token.side_effect = encrypt_side_effect
    mock.decrypt_token.side_effect = decrypt_side_effect
    return mock


@pytest.fixture
def crypto_with_fernet(mock_oauth_crypto: MagicMock) -> IdentityCrypto:
    """IdentityCrypto with mock Fernet for full key lifecycle."""
    return IdentityCrypto(oauth_crypto=mock_oauth_crypto)


# ---------------------------------------------------------------------------
# Unit Tests — Keypair Generation
# ---------------------------------------------------------------------------


class TestKeypairGeneration:
    def test_generate_keypair_returns_key_pair(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, public = crypto_no_fernet.generate_keypair()
        assert isinstance(private, Ed25519PrivateKey)
        assert isinstance(public, Ed25519PublicKey)

    def test_generate_keypair_unique(self, crypto_no_fernet: IdentityCrypto) -> None:
        """Each call generates a unique keypair."""
        _, pub1 = crypto_no_fernet.generate_keypair()
        _, pub2 = crypto_no_fernet.generate_keypair()
        bytes1 = IdentityCrypto.public_key_to_bytes(pub1)
        bytes2 = IdentityCrypto.public_key_to_bytes(pub2)
        assert bytes1 != bytes2

    def test_public_key_is_32_bytes(self, crypto_no_fernet: IdentityCrypto) -> None:
        _, public = crypto_no_fernet.generate_keypair()
        raw = IdentityCrypto.public_key_to_bytes(public)
        assert len(raw) == 32

    def test_public_key_roundtrip(self, crypto_no_fernet: IdentityCrypto) -> None:
        _, public = crypto_no_fernet.generate_keypair()
        raw = IdentityCrypto.public_key_to_bytes(public)
        restored = IdentityCrypto.public_key_from_bytes(raw)
        assert IdentityCrypto.public_key_to_bytes(restored) == raw


# ---------------------------------------------------------------------------
# Unit Tests — Signing and Verification
# ---------------------------------------------------------------------------


class TestSignVerify:
    def test_sign_and_verify_valid(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, public = crypto_no_fernet.generate_keypair()
        message = b"hello agent world"
        signature = crypto_no_fernet.sign(message, private)
        assert crypto_no_fernet.verify(message, signature, public) is True

    def test_verify_rejects_tampered_message(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, public = crypto_no_fernet.generate_keypair()
        message = b"original message"
        signature = crypto_no_fernet.sign(message, private)
        assert crypto_no_fernet.verify(b"tampered message", signature, public) is False

    def test_verify_rejects_wrong_key(self, crypto_no_fernet: IdentityCrypto) -> None:
        private1, _ = crypto_no_fernet.generate_keypair()
        _, public2 = crypto_no_fernet.generate_keypair()
        message = b"cross-agent test"
        signature = crypto_no_fernet.sign(message, private1)
        assert crypto_no_fernet.verify(message, signature, public2) is False

    def test_signature_is_64_bytes(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, _ = crypto_no_fernet.generate_keypair()
        signature = crypto_no_fernet.sign(b"test", private)
        assert len(signature) == 64

    def test_sign_empty_message(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, public = crypto_no_fernet.generate_keypair()
        signature = crypto_no_fernet.sign(b"", private)
        assert crypto_no_fernet.verify(b"", signature, public) is True


# ---------------------------------------------------------------------------
# Unit Tests — Private Key Encryption
# ---------------------------------------------------------------------------


class TestPrivateKeyEncryption:
    def test_encrypt_decrypt_roundtrip(self, crypto_with_fernet: IdentityCrypto) -> None:
        private, public = crypto_with_fernet.generate_keypair()
        encrypted = crypto_with_fernet.encrypt_private_key(private)
        decrypted = crypto_with_fernet.decrypt_private_key(encrypted)

        # Verify the decrypted key can sign and original public key can verify
        message = b"roundtrip test"
        signature = crypto_with_fernet.sign(message, decrypted)
        assert crypto_with_fernet.verify(message, signature, public) is True

    def test_encrypt_without_oauth_crypto_raises(self, crypto_no_fernet: IdentityCrypto) -> None:
        private, _ = crypto_no_fernet.generate_keypair()
        with pytest.raises(ValueError, match="OAuthCrypto is required"):
            crypto_no_fernet.encrypt_private_key(private)

    def test_decrypt_without_oauth_crypto_raises(self, crypto_no_fernet: IdentityCrypto) -> None:
        with pytest.raises(ValueError, match="OAuthCrypto is required"):
            crypto_no_fernet.decrypt_private_key("some_encrypted_data")

    def test_encrypted_key_is_string(self, crypto_with_fernet: IdentityCrypto) -> None:
        private, _ = crypto_with_fernet.generate_keypair()
        encrypted = crypto_with_fernet.encrypt_private_key(private)
        assert isinstance(encrypted, str)
        assert len(encrypted) > 0


# ---------------------------------------------------------------------------
# Unit Tests — Public Key Serialization
# ---------------------------------------------------------------------------


class TestPublicKeySerialization:
    def test_from_bytes_rejects_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="must be 32 bytes"):
            IdentityCrypto.public_key_from_bytes(b"\x00" * 16)

    def test_from_bytes_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must be 32 bytes"):
            IdentityCrypto.public_key_from_bytes(b"")

    def test_from_bytes_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="must be 32 bytes"):
            IdentityCrypto.public_key_from_bytes(b"\x00" * 64)


# ---------------------------------------------------------------------------
# Hypothesis Property-Based Tests (Decision #9B)
# ---------------------------------------------------------------------------


class TestCryptoProperties:
    """Property-based tests for cryptographic roundtrip invariants."""

    @given(message=st.binary(min_size=0, max_size=10_000))
    @settings(max_examples=100)
    def test_sign_verify_roundtrip(self, message: bytes) -> None:
        """For any message, sign(msg, sk) verified by pk is always True."""
        crypto = IdentityCrypto()
        private, public = crypto.generate_keypair()
        signature = crypto.sign(message, private)
        assert crypto.verify(message, signature, public) is True

    @given(message=st.binary(min_size=1, max_size=10_000))
    @settings(max_examples=50)
    def test_tampering_always_detected(self, message: bytes) -> None:
        """Flipping any bit in the message invalidates the signature."""
        crypto = IdentityCrypto()
        private, public = crypto.generate_keypair()
        signature = crypto.sign(message, private)

        # Flip the first byte
        tampered = bytes([message[0] ^ 0xFF]) + message[1:]
        assert crypto.verify(tampered, signature, public) is False

    @given(data=st.data())
    @settings(max_examples=50)
    def test_public_key_bytes_roundtrip(self, data: st.DataObject) -> None:
        """public_key_from_bytes(public_key_to_bytes(pk)) == pk."""
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        raw = IdentityCrypto.public_key_to_bytes(public)
        restored = IdentityCrypto.public_key_from_bytes(raw)
        assert IdentityCrypto.public_key_to_bytes(restored) == raw

    @given(message=st.binary(min_size=0, max_size=1000))
    @settings(max_examples=50)
    def test_cross_key_rejection(self, message: bytes) -> None:
        """Agent A's signature is never verified by Agent B's key."""
        crypto = IdentityCrypto()
        private_a, _ = crypto.generate_keypair()
        _, public_b = crypto.generate_keypair()
        signature = crypto.sign(message, private_a)
        assert crypto.verify(message, signature, public_b) is False

    @given(message=st.binary(min_size=0, max_size=1000))
    @settings(max_examples=50)
    def test_encrypt_decrypt_private_key_roundtrip(self, message: bytes) -> None:
        """Encrypted private key decrypts to a functionally equivalent key."""
        mock_oauth = MagicMock()
        store: dict[str, str] = {}

        def encrypt(token: str) -> str:
            enc = f"E:{token}"
            store[enc] = token
            return enc

        def decrypt(enc: str) -> str:
            return store[enc]

        mock_oauth.encrypt_token.side_effect = encrypt
        mock_oauth.decrypt_token.side_effect = decrypt

        crypto = IdentityCrypto(oauth_crypto=mock_oauth)
        private, public = crypto.generate_keypair()

        encrypted = crypto.encrypt_private_key(private)
        decrypted = crypto.decrypt_private_key(encrypted)

        # Verify functional equivalence: decrypted key can sign, original pk verifies
        sig = crypto.sign(message, decrypted)
        assert crypto.verify(message, sig, public) is True
