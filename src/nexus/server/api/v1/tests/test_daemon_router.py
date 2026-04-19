"""Tests for src/nexus/server/api/v1/routers/daemon.py."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.tests.test_postgres_profile_store import (
    ensure_principal,
    ensure_tenant,
)
from nexus.server.api.v1.enroll_tokens import issue_enroll_token
from nexus.server.api.v1.jwt_signer import JwtSigner
from nexus.server.api.v1.routers.daemon import make_daemon_router

SECRET = b"enroll-secret-32bytes-abcdef01234"


@pytest.fixture
def signing_pem() -> bytes:
    k = ec.generate_private_key(ec.SECP256R1())
    return k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def app(pg_engine: Engine, signing_pem: bytes) -> FastAPI:
    signer = JwtSigner.from_pem(signing_pem, issuer="https://test.nexus")
    router = make_daemon_router(engine=pg_engine, signer=signer, enroll_secret=SECRET)
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def tenant_principal(pg_engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    t = ensure_tenant(pg_engine, f"daemon-rt-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine,
        tenant_id=t,
        external_sub=f"u-{uuid.uuid4()}",
        auth_method="oidc",
    )
    return t, p


def _machine_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def test_enroll_happy_path(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    _, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "laptop-01",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "machine_id" in body
    assert "jwt" in body
    assert "server_pubkey_pem" in body


def test_enroll_replay_rejected(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    _, pub_pem = _machine_keypair()
    r1 = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    assert r2.status_code == 409
    assert "reused" in r2.text


def test_enroll_bad_token(client: TestClient) -> None:
    _, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": "garbage.xxx",
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    assert r.status_code == 401


def test_refresh_happy_path(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    priv, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    machine_id = r.json()["machine_id"]
    body = {
        "machine_id": machine_id,
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = priv.sign(body_bytes)
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body": body, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 200, r2.text
    assert "jwt" in r2.json()


def test_refresh_signature_mismatch(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    _priv, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    machine_id = r.json()["machine_id"]
    body = {
        "machine_id": machine_id,
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    # Forge a signature with a different key
    other = ed25519.Ed25519PrivateKey.generate()
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = other.sign(body_bytes)
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body": body, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401


def test_refresh_skew_rejected(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    priv, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    machine_id = r.json()["machine_id"]
    # Timestamp 10 minutes in the past — outside +/-60s window
    skewed = datetime.now(UTC) - timedelta(minutes=10)
    body = {"machine_id": machine_id, "timestamp_utc": skewed.isoformat()}
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = priv.sign(body_bytes)
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body": body, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert "skew" in r2.text.lower()


def test_refresh_revoked_machine(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    priv, pub_pem = _machine_keypair()
    r = client.post(
        "/v1/daemon/enroll",
        json={
            "enroll_token": tok,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": "0.9.20",
            "hostname": "x",
        },
    )
    machine_id = r.json()["machine_id"]
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        conn.execute(
            text("UPDATE daemon_machines SET revoked_at = NOW() WHERE id = :m"),
            {"m": machine_id},
        )
    body = {
        "machine_id": machine_id,
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = priv.sign(body_bytes)
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body": body, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert "revoked" in r2.text.lower()
