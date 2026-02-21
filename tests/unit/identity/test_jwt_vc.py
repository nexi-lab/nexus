"""Unit tests for JWT-VC credential issuance and verification (Issue #1753).

Uses real Ed25519 crypto (no mocks) for end-to-end JWT integrity.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.contracts.credential_types import (
    CredentialStatus,
    CredentialVerificationResult,
    VerifiableCredential,
)
from nexus.identity.credentials import (
    JWTCredentialIssuer,
    JWTCredentialVerifier,
    _b64url_decode,
    _b64url_encode,
)
from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import create_did_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeKeyRecord:
    """Fake key record for testing."""

    def __init__(self, key_id: str, did: str) -> None:
        self.key_id = key_id
        self.did = did


class FakeKeyService:
    """Minimal KeyService substitute using real crypto."""

    def __init__(self, crypto: IdentityCrypto) -> None:
        self._crypto = crypto
        self._keys: dict[str, tuple] = {}  # agent_id -> (private, public, did, key_id)

    def ensure_keypair(self, agent_id: str) -> FakeKeyRecord:
        if agent_id not in self._keys:
            priv, pub = self._crypto.generate_keypair()
            did = create_did_key(pub)
            key_id = f"key-{agent_id}"
            self._keys[agent_id] = (priv, pub, did, key_id)
        _, _, did, key_id = self._keys[agent_id]
        return FakeKeyRecord(key_id=key_id, did=did)

    def decrypt_private_key(self, key_id: str) -> object:
        for _, (priv, _, _, kid) in self._keys.items():
            if kid == key_id:
                return priv
        raise ValueError(f"Key {key_id} not found")


@pytest.fixture()
def crypto() -> IdentityCrypto:
    return IdentityCrypto(oauth_crypto=None)


@pytest.fixture()
def key_service(crypto: IdentityCrypto) -> FakeKeyService:
    return FakeKeyService(crypto)


@pytest.fixture()
def issuer(key_service: FakeKeyService, crypto: IdentityCrypto) -> JWTCredentialIssuer:
    return JWTCredentialIssuer(key_service=key_service, crypto=crypto)


@pytest.fixture()
def verifier(key_service: FakeKeyService, crypto: IdentityCrypto) -> JWTCredentialVerifier:
    return JWTCredentialVerifier(key_service=key_service, crypto=crypto)


# ---------------------------------------------------------------------------
# Tests: Base64url helpers
# ---------------------------------------------------------------------------


class TestBase64url:
    def test_roundtrip(self) -> None:
        data = b"hello world"
        encoded = _b64url_encode(data)
        assert _b64url_decode(encoded) == data

    def test_no_padding(self) -> None:
        encoded = _b64url_encode(b"test")
        assert "=" not in encoded

    def test_url_safe(self) -> None:
        data = bytes(range(256))
        encoded = _b64url_encode(data)
        assert "+" not in encoded
        assert "/" not in encoded


# ---------------------------------------------------------------------------
# Tests: JWTCredentialIssuer
# ---------------------------------------------------------------------------


class TestJWTCredentialIssuer:
    def test_issue_returns_vc_and_jws(self, issuer: JWTCredentialIssuer) -> None:
        vc, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"mcp:read_file", "mcp:search"}),
        )

        assert isinstance(vc, VerifiableCredential)
        assert isinstance(jws, str)
        assert vc.jws_compact == jws
        assert vc.status == CredentialStatus.ACTIVE

    def test_vc_structure(self, issuer: JWTCredentialIssuer) -> None:
        vc, _ = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"mcp:read_file"}),
        )

        assert vc.id.startswith("urn:uuid:")
        assert vc.type == ("VerifiableCredential", "AgentCapabilityCredential")
        assert vc.issuer.startswith("did:key:")
        assert vc.credential_subject.type == "AgentCapability"
        assert "mcp:read_file" in vc.credential_subject.capabilities
        assert vc.proof is not None
        assert vc.proof.type == "Ed25519Signature2020"

    def test_jws_has_three_parts(self, issuer: JWTCredentialIssuer) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
        )
        parts = jws.split(".")
        assert len(parts) == 3

    def test_jws_header_alg_eddsa(self, issuer: JWTCredentialIssuer) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
        )
        header_b64 = jws.split(".")[0]
        header = json.loads(_b64url_decode(header_b64))
        assert header["alg"] == "EdDSA"
        assert header["typ"] == "JWT"

    def test_jws_payload_contains_vc(self, issuer: JWTCredentialIssuer) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
        )
        payload_b64 = jws.split(".")[1]
        payload = json.loads(_b64url_decode(payload_b64))
        assert "vc" in payload
        assert "iss" in payload
        assert "sub" in payload
        assert "exp" in payload
        assert "jti" in payload

    def test_constraints_included(self, issuer: JWTCredentialIssuer) -> None:
        vc, _ = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
            constraints={"max_file_size": 10485760},
        )
        assert vc.credential_subject.constraints == {"max_file_size": 10485760}

    def test_custom_valid_hours(self, issuer: JWTCredentialIssuer) -> None:
        vc, _ = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
            valid_hours=1,
        )
        assert vc.valid_until is not None


# ---------------------------------------------------------------------------
# Tests: JWTCredentialVerifier
# ---------------------------------------------------------------------------


class TestJWTCredentialVerifier:
    def test_verify_valid_credential(
        self,
        issuer: JWTCredentialIssuer,
        verifier: JWTCredentialVerifier,
    ) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"mcp:read_file"}),
        )

        result = verifier.verify(jws)
        assert result.valid is True
        assert len(result.errors) == 0
        assert result.issuer_did.startswith("did:key:")

    def test_tampered_signature_fails(
        self,
        issuer: JWTCredentialIssuer,
        verifier: JWTCredentialVerifier,
    ) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
        )

        # Tamper with signature
        parts = jws.split(".")
        tampered_sig = parts[2][:-4] + "XXXX"
        tampered_jws = f"{parts[0]}.{parts[1]}.{tampered_sig}"

        result = verifier.verify(tampered_jws)
        assert result.valid is False
        assert any("signature" in e.lower() or "cannot resolve" in e.lower() for e in result.errors)

    def test_tampered_payload_fails(
        self,
        issuer: JWTCredentialIssuer,
        verifier: JWTCredentialVerifier,
    ) -> None:
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
        )

        # Replace payload with different content
        parts = jws.split(".")
        fake_payload = _b64url_encode(
            json.dumps({"iss": "did:key:fake", "exp": 99999999999}).encode()
        )
        tampered_jws = f"{parts[0]}.{fake_payload}.{parts[2]}"

        result = verifier.verify(tampered_jws)
        assert result.valid is False

    def test_invalid_jws_format(self, verifier: JWTCredentialVerifier) -> None:
        result = verifier.verify("not.a.valid.jws.at.all")
        assert result.valid is False

    def test_two_part_jws(self, verifier: JWTCredentialVerifier) -> None:
        result = verifier.verify("only.two")
        assert result.valid is False
        assert "expected 3 parts" in result.errors[0].lower()

    def test_expired_credential(
        self,
        issuer: JWTCredentialIssuer,
        verifier: JWTCredentialVerifier,
    ) -> None:
        # Issue with very short expiry and then fake time
        _, jws = issuer.issue(
            issuer_agent_id="issuer-1",
            subject_agent_id="subject-1",
            capabilities=frozenset({"cap:a"}),
            valid_hours=1,
        )

        # Verify the original with mocked time set far in the future
        with patch("nexus.identity.credentials.datetime") as mock_dt:
            from datetime import UTC, datetime

            future = datetime(2099, 1, 1, tzinfo=UTC)
            mock_dt.now.return_value = future
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = verifier.verify(jws)
            assert result.valid is False
            assert any("expired" in e.lower() for e in result.errors)

    def test_bad_base64_header(self, verifier: JWTCredentialVerifier) -> None:
        result = verifier.verify("!!!.valid.sig")
        assert result.valid is False

    def test_missing_issuer(self, verifier: JWTCredentialVerifier) -> None:
        header = _b64url_encode(json.dumps({"alg": "EdDSA"}).encode())
        payload = _b64url_encode(json.dumps({"sub": "test"}).encode())
        sig = _b64url_encode(b"\x00" * 64)
        result = verifier.verify(f"{header}.{payload}.{sig}")
        assert result.valid is False
        assert any("missing issuer" in e.lower() for e in result.errors)

    def test_unsupported_algorithm(self, verifier: JWTCredentialVerifier) -> None:
        header = _b64url_encode(json.dumps({"alg": "RS256"}).encode())
        payload = _b64url_encode(json.dumps({"iss": "did:key:z6Mktest", "sub": "test"}).encode())
        sig = _b64url_encode(b"\x00" * 64)
        result = verifier.verify(f"{header}.{payload}.{sig}")
        assert result.valid is False
        assert any("unsupported" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: CredentialVerificationResult immutability
# ---------------------------------------------------------------------------


class TestCredentialTypes:
    def test_vc_frozen(self) -> None:
        from nexus.contracts.credential_types import CredentialSubject, VerifiableCredential

        subject = CredentialSubject(
            id="did:key:test",
            type="AgentCapability",
            capabilities=frozenset({"cap:a"}),
            constraints={},
        )
        vc = VerifiableCredential(
            id="urn:uuid:test",
            type=("VerifiableCredential",),
            issuer="did:key:issuer",
            valid_from="2026-01-01T00:00:00",
            valid_until=None,
            credential_subject=subject,
            proof=None,
            status=CredentialStatus.ACTIVE,
        )
        with pytest.raises(AttributeError):
            vc.id = "changed"

    def test_verification_result_frozen(self) -> None:
        result = CredentialVerificationResult(
            valid=True,
            credential_id="test",
            issuer_did="did:key:test",
            errors=(),
            checked_at="2026-01-01T00:00:00",
        )
        with pytest.raises(AttributeError):
            result.valid = False

    def test_status_enum_values(self) -> None:
        assert CredentialStatus.ACTIVE == "active"
        assert CredentialStatus.REVOKED == "revoked"
        assert CredentialStatus.EXPIRED == "expired"
        assert CredentialStatus.SUSPENDED == "suspended"
