"""Tests for DID generation and resolution (Issue #1355).

Includes:
- Traditional unit tests for did:key and did:web
- Hypothesis property-based tests for encoding roundtrips (Decision #9B)
- Base58 encoding edge cases
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import (
    _base58_decode,
    _base58_encode,
    create_did_document,
    create_did_key,
    create_did_web,
    resolve_did_key,
)

# ---------------------------------------------------------------------------
# Unit Tests — did:key
# ---------------------------------------------------------------------------


class TestCreateDidKey:
    def test_format(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        assert did.startswith("did:key:z")

    def test_unique_per_key(self) -> None:
        crypto = IdentityCrypto()
        _, pub1 = crypto.generate_keypair()
        _, pub2 = crypto.generate_keypair()
        assert create_did_key(pub1) != create_did_key(pub2)

    def test_deterministic(self) -> None:
        """Same key always produces the same DID."""
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        assert create_did_key(public) == create_did_key(public)

    def test_did_length_reasonable(self) -> None:
        """did:key for Ed25519 should be ~56 chars."""
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        # "did:key:z" = 9 chars + ~46 chars base58 = ~55 total
        assert 50 < len(did) < 70


class TestResolveDidKey:
    def test_roundtrip(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        resolved = resolve_did_key(did)
        assert IdentityCrypto.public_key_to_bytes(resolved) == IdentityCrypto.public_key_to_bytes(
            public
        )

    def test_invalid_prefix(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            resolve_did_key("did:web:example.com")

    def test_empty_identifier(self) -> None:
        with pytest.raises(ValueError, match="Empty did:key"):
            resolve_did_key("did:key:z")

    def test_wrong_multicodec(self) -> None:
        """Non-Ed25519 multicodec prefix should be rejected."""
        # Create a did:key with wrong prefix (0x00 0x01 instead of 0xed 0x01)
        fake_data = bytes([0x00, 0x01]) + b"\x00" * 32
        encoded = _base58_encode(fake_data)
        with pytest.raises(ValueError, match="Unexpected multicodec"):
            resolve_did_key(f"did:key:z{encoded}")

    def test_wrong_key_length(self) -> None:
        """Payload with wrong key length should be rejected."""
        from nexus.identity.did import _ED25519_MULTICODEC_PREFIX

        fake_data = _ED25519_MULTICODEC_PREFIX + b"\x00" * 16  # 16 bytes, not 32
        encoded = _base58_encode(fake_data)
        with pytest.raises(ValueError, match="must be 32 bytes"):
            resolve_did_key(f"did:key:z{encoded}")


# ---------------------------------------------------------------------------
# Unit Tests — did:web
# ---------------------------------------------------------------------------


class TestCreateDidWeb:
    def test_format(self) -> None:
        did = create_did_web("nexus.sudorouter.ai", "agent123")
        assert did == "did:web:nexus.sudorouter.ai:agents:agent123"

    def test_comma_in_agent_id(self) -> None:
        did = create_did_web("example.com", "alice,ImpersonatedUser")
        assert did == "did:web:example.com:agents:alice-ImpersonatedUser"

    def test_slash_in_agent_id(self) -> None:
        did = create_did_web("example.com", "path/agent")
        assert did == "did:web:example.com:agents:path-agent"

    def test_empty_domain_raises(self) -> None:
        with pytest.raises(ValueError, match="domain is required"):
            create_did_web("", "agent123")

    def test_empty_agent_id_raises(self) -> None:
        with pytest.raises(ValueError, match="agent_id is required"):
            create_did_web("example.com", "")


# ---------------------------------------------------------------------------
# Unit Tests — DID Document
# ---------------------------------------------------------------------------


class TestCreateDidDocument:
    def test_basic_structure(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        doc = create_did_document(did, public)

        assert doc["id"] == did
        assert doc["controller"] == did
        assert len(doc["verificationMethod"]) == 1
        assert doc["verificationMethod"][0]["type"] == "Ed25519VerificationKey2020"
        assert f"{did}#key-1" in doc["authentication"]
        assert f"{did}#key-1" in doc["assertionMethod"]

    def test_custom_controller(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        owner_did = "did:key:z6MkOwner..."
        doc = create_did_document(did, public, controller=owner_did)
        assert doc["controller"] == owner_did

    def test_service_endpoints(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        doc = create_did_document(
            did,
            public,
            service_endpoints={"AgentService": "https://example.com/agent"},
        )
        assert "service" in doc
        assert len(doc["service"]) == 1
        assert doc["service"][0]["type"] == "AgentService"

    def test_no_service_endpoints(self) -> None:
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        doc = create_did_document(did, public)
        assert "service" not in doc


# ---------------------------------------------------------------------------
# Unit Tests — Base58 Encoding/Decoding
# ---------------------------------------------------------------------------


class TestBase58:
    def test_roundtrip_simple(self) -> None:
        data = b"hello"
        assert _base58_decode(_base58_encode(data)) == data

    def test_roundtrip_zeros(self) -> None:
        """Leading zero bytes are preserved."""
        data = b"\x00\x00\x01"
        assert _base58_decode(_base58_encode(data)) == data

    def test_roundtrip_all_zeros(self) -> None:
        data = b"\x00" * 5
        assert _base58_decode(_base58_encode(data)) == data

    def test_empty(self) -> None:
        assert _base58_encode(b"") == ""
        assert _base58_decode("") == b""

    def test_invalid_char(self) -> None:
        with pytest.raises(ValueError, match="Invalid base58"):
            _base58_decode("O")  # 'O' is not in base58 alphabet

    def test_invalid_char_zero(self) -> None:
        with pytest.raises(ValueError, match="Invalid base58"):
            _base58_decode("0")  # '0' is not in base58 alphabet


# ---------------------------------------------------------------------------
# Hypothesis Property-Based Tests (Decision #9B)
# ---------------------------------------------------------------------------


class TestDidProperties:
    @given(data=st.data())
    @settings(max_examples=100)
    def test_did_key_roundtrip(self, data: st.DataObject) -> None:
        """create_did_key → resolve_did_key always returns the same public key."""
        crypto = IdentityCrypto()
        _, public = crypto.generate_keypair()
        did = create_did_key(public)
        resolved = resolve_did_key(did)
        assert IdentityCrypto.public_key_to_bytes(resolved) == IdentityCrypto.public_key_to_bytes(
            public
        )

    @given(data=st.binary(min_size=1, max_size=100))
    @settings(max_examples=100)
    def test_base58_roundtrip(self, data: bytes) -> None:
        """base58_decode(base58_encode(data)) == data for arbitrary bytes."""
        assert _base58_decode(_base58_encode(data)) == data

    @given(data=st.binary(min_size=0, max_size=10).filter(lambda b: b == b"\x00" * len(b)))
    @settings(max_examples=20)
    def test_base58_leading_zeros_preserved(self, data: bytes) -> None:
        """Leading zero bytes are correctly preserved in base58 roundtrip."""
        assert _base58_decode(_base58_encode(data)) == data
