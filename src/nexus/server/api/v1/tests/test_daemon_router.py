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


def _canonical_body(
    *, machine_id: str, tenant_id: str, timestamp_utc: str, nonce: str | None = None
) -> str:
    """Client-side canonicalization shared by all refresh tests."""
    return json.dumps(
        {
            "machine_id": machine_id,
            "nonce": nonce or str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "timestamp_utc": timestamp_utc,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


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
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
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
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    # Forge a signature with a different key
    other = ed25519.Ed25519PrivateKey.generate()
    sig = other.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert "signature_invalid" in r2.text


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
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=skewed.isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
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
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert "revoked" in r2.text.lower()


def test_refresh_bad_body_raw_json(client: TestClient) -> None:
    """Non-JSON ``body_raw`` → 401 body_malformed."""
    priv = ed25519.Ed25519PrivateKey.generate()
    body_raw = "not-json"
    sig = priv.sign(body_raw.encode("utf-8"))
    r = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r.status_code == 401
    assert "body_malformed" in r.text


def test_refresh_tenant_mismatch(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A ``body_raw`` claiming a different tenant than the machine's real tenant
    → 401 machine_unknown (tenant-scoped SELECT finds nothing)."""
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
    # Forge: sign with the legit machine key but claim a *different* tenant.
    bogus_tenant = uuid.uuid4()
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(bogus_tenant),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert "machine_unknown" in r2.text


def test_refresh_replay_rejected(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Re-submitting the exact same (body_raw, sig) must 401 nonce_replay.

    Without the nonce guard, a captured request could be replayed inside
    the ±60s skew window to mint fresh JWTs. This test captures a signed
    body, uses it once, then submits it a second time verbatim.
    """
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
    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    payload = {"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()}
    r1 = client.post("/v1/daemon/refresh", json=payload)
    assert r1.status_code == 200
    # Replay with identical body_raw + sig_b64 → rejected.
    r2 = client.post("/v1/daemon/refresh", json=payload)
    assert r2.status_code == 401
    assert r2.json()["detail"] == "nonce_replay"


def test_refresh_missing_nonce_rejected(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Legacy client that omits ``nonce`` from body must 401 body_malformed."""
    t, p = tenant_principal
    tok = issue_enroll_token(
        engine=pg_engine, secret=SECRET, tenant_id=t, principal_id=p, ttl=timedelta(minutes=15)
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
    # Hand-craft a body without ``nonce`` (legacy shape).
    body_raw = json.dumps(
        {
            "machine_id": machine_id,
            "tenant_id": str(t),
            "timestamp_utc": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r2 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r2.status_code == 401
    assert r2.json()["detail"] == "body_malformed"


def test_refresh_prunes_old_nonce_rows(
    client: TestClient,
    pg_engine: Engine,
    tenant_principal: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful refresh prunes nonce rows older than the retention window.

    Regression: without pruning the ``daemon_refresh_nonces`` table grows
    unbounded (one row per refresh, forever). We force the sampled prune
    branch to run deterministically and insert a synthetic row outside the
    retention window, then assert the refresh deletes it.
    """
    from nexus.server.api.v1.routers import daemon as daemon_router_mod

    # Force the opportunistic prune to run on this refresh.
    monkeypatch.setattr(daemon_router_mod.secrets, "randbelow", lambda _n: 0)

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

    # Seed a synthetic "ancient" nonce row. seen_at is backdated well past
    # the retention window so a correct prune must delete it.
    ancient_nonce = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(t)},
        )
        conn.execute(
            text(
                "INSERT INTO daemon_refresh_nonces "
                "(nonce, tenant_id, machine_id, seen_at) "
                "VALUES (:n, :t, :m, NOW() - INTERVAL '1 day')"
            ),
            {"n": str(ancient_nonce), "t": str(t), "m": machine_id},
        )

    body_raw = _canonical_body(
        machine_id=machine_id,
        tenant_id=str(t),
        timestamp_utc=datetime.now(UTC).isoformat(),
    )
    sig = priv.sign(body_raw.encode("utf-8"))
    r1 = client.post(
        "/v1/daemon/refresh",
        json={"body_raw": body_raw, "sig_b64": base64.b64encode(sig).decode()},
    )
    assert r1.status_code == 200, r1.text

    # The ancient row should have been pruned by this refresh; the fresh row
    # inserted by this refresh should still be present (inside retention).
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        ancient_remaining = conn.execute(
            text("SELECT 1 FROM daemon_refresh_nonces WHERE tenant_id = :t AND nonce = :n"),
            {"t": str(t), "n": str(ancient_nonce)},
        ).fetchone()
        fresh_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM daemon_refresh_nonces "
                "WHERE tenant_id = :t AND machine_id = :m "
                "AND seen_at > NOW() - INTERVAL '1 minute'"
            ),
            {"t": str(t), "m": machine_id},
        ).scalar()
    assert ancient_remaining is None
    assert fresh_count == 1
