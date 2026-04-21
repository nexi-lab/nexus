"""Tests for src/nexus/server/api/v1/routers/jwks.py."""

from __future__ import annotations

import base64

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v1.jwt_signer import JwtSigner
from nexus.server.api.v1.routers.jwks import make_jwks_router


@pytest.fixture
def signing_pem() -> bytes:
    k = ec.generate_private_key(ec.SECP256R1())
    return k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def signer(signing_pem: bytes) -> JwtSigner:
    return JwtSigner.from_pem(signing_pem, issuer="https://test.nexus")


@pytest.fixture
def client(signer: JwtSigner) -> TestClient:
    app = FastAPI()
    app.include_router(make_jwks_router(signer=signer))
    return TestClient(app)


def test_jwks_shape(client: TestClient) -> None:
    r = client.get("/v1/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    assert "keys" in body and len(body["keys"]) == 1
    jwk = body["keys"][0]
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert jwk["alg"] == "ES256"
    assert jwk["use"] == "sig"
    # x, y are base64url-encoded 32-byte big-endian coords (no padding)
    x = base64.urlsafe_b64decode(jwk["x"] + "==")
    y = base64.urlsafe_b64decode(jwk["y"] + "==")
    assert len(x) == 32
    assert len(y) == 32


def test_jwks_verifies_a_real_token(client: TestClient, signer: JwtSigner) -> None:
    """Round-trip: publish JWK, re-derive public key, verify a real signed JWT."""
    import uuid
    from datetime import timedelta

    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        EllipticCurvePublicNumbers,
    )

    from nexus.server.api.v1.jwt_signer import DaemonClaims

    token = signer.sign(
        DaemonClaims(tenant_id=uuid.uuid4(), principal_id=uuid.uuid4(), machine_id=uuid.uuid4()),
        ttl=timedelta(minutes=5),
    )
    jwk = client.get("/v1/.well-known/jwks.json").json()["keys"][0]
    x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=="), "big")
    y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=="), "big")
    pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key()
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    decoded = pyjwt.decode(
        token,
        pub_pem,
        algorithms=["ES256"],
        audience="nexus-daemon",
        issuer="https://test.nexus",
    )
    assert decoded["aud"] == "nexus-daemon"


def test_public_key_jwk_on_signer(signer: JwtSigner) -> None:
    """JwtSigner.public_key_jwk exposes a JWK dict with x/y coords."""
    jwk = signer.public_key_jwk()
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    assert len(base64.urlsafe_b64decode(jwk["x"] + "==")) == 32
    assert len(base64.urlsafe_b64decode(jwk["y"] + "==")) == 32
