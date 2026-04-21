"""Tests for src/nexus/server/api/v1/jwt_signer.py."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nexus.server.api.v1.jwt_signer import (
    DaemonClaims,
    JwtSigner,
    JwtVerifyError,
)


@pytest.fixture
def signing_key_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def signer(signing_key_pem: bytes) -> JwtSigner:
    return JwtSigner.from_pem(signing_key_pem, issuer="https://test.nexus")


def test_round_trip(signer: JwtSigner) -> None:
    claims = DaemonClaims(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
    )
    jwt_str = signer.sign(claims, ttl=timedelta(hours=1))
    decoded = signer.verify(jwt_str)
    assert decoded.tenant_id == claims.tenant_id
    assert decoded.principal_id == claims.principal_id
    assert decoded.machine_id == claims.machine_id


def test_expired_token_rejected(signer: JwtSigner) -> None:
    claims = DaemonClaims(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
    )
    jwt_str = signer.sign(claims, ttl=timedelta(seconds=-5))
    with pytest.raises(JwtVerifyError, match="expired"):
        signer.verify(jwt_str)


def test_tampered_token_rejected(signer: JwtSigner) -> None:
    claims = DaemonClaims(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
    )
    jwt_str = signer.sign(claims, ttl=timedelta(hours=1))
    tampered = jwt_str[:-4] + "AAAA"
    with pytest.raises(JwtVerifyError):
        signer.verify(tampered)


def test_wrong_issuer_rejected(signer: JwtSigner, signing_key_pem: bytes) -> None:
    claims = DaemonClaims(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
    )
    other_signer = JwtSigner.from_pem(signing_key_pem, issuer="https://other.nexus")
    jwt_str = other_signer.sign(claims, ttl=timedelta(hours=1))
    with pytest.raises(JwtVerifyError, match="issuer"):
        signer.verify(jwt_str)
