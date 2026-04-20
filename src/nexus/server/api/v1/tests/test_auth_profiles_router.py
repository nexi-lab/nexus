"""Tests for src/nexus/server/api/v1/routers/auth_profiles.py."""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.tests.test_postgres_profile_store import (
    ensure_principal,
    ensure_tenant,
)
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.auth_profiles import make_auth_profiles_router


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
def app(pg_engine: Engine, signer: JwtSigner) -> FastAPI:
    a = FastAPI()
    a.include_router(make_auth_profiles_router(engine=pg_engine, signer=signer))
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def setup_tenant(pg_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    import os

    t = ensure_tenant(pg_engine, f"push-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine,
        tenant_id=t,
        external_sub=f"u-{uuid.uuid4()}",
        auth_method="oidc",
    )
    m = uuid.uuid4()
    # daemon_machines.pubkey has a GLOBAL unique index (perf + correctness for
    # refresh lookups). Tests share a module-scoped engine, so a fixed value
    # like b"\x00" * 32 trips the constraint on the second function-scoped
    # fixture call. Generate a random 32-byte key per test instead.
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        conn.execute(
            text(
                "INSERT INTO daemon_machines "
                "(id, tenant_id, principal_id, pubkey, daemon_version_last_seen, "
                " enrolled_at, last_seen_at) "
                "VALUES (:id, :tid, :pid, :pk, :ver, NOW(), NOW())"
            ),
            {
                "id": str(m),
                "tid": str(t),
                "pid": str(p),
                "pk": os.urandom(32),
                "ver": "0.9.20",
            },
        )
    return t, p, m


def _push_payload(provider: str = "codex") -> dict:
    return {
        "id": f"{provider}/user@example.com",
        "provider": provider,
        "account_identifier": "user@example.com",
        "backend": "nexus-token-manager",
        "backend_key": "codex-1",
        "envelope": {
            "ciphertext_b64": base64.b64encode(b"\x01" * 32).decode(),
            "wrapped_dek_b64": base64.b64encode(b"\x02" * 48).decode(),
            "nonce_b64": base64.b64encode(b"\x03" * 12).decode(),
            "aad_b64": base64.b64encode(b"\x04" * 16).decode(),
            "kek_version": 1,
        },
        "source_file_hash": "deadbeef" * 8,
    }


def test_push_happy_path(
    client: TestClient,
    setup_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    signer: JwtSigner,
    pg_engine: Engine,
) -> None:
    t, p, m = setup_tenant
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    r = client.post(
        "/v1/auth-profiles",
        json=_push_payload(),
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r.status_code == 200, r.text

    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        row = conn.execute(
            text(
                "SELECT source_file_hash, daemon_version, machine_id, ciphertext "
                "FROM auth_profiles "
                "WHERE tenant_id = :t AND principal_id = :p AND id = :pid"
            ),
            {"t": t, "p": p, "pid": "codex/user@example.com"},
        ).fetchone()
    assert row is not None
    assert row.source_file_hash == "deadbeef" * 8
    assert row.machine_id == m
    assert bytes(row.ciphertext) == b"\x01" * 32


def test_push_writes_audit_row(
    client: TestClient,
    setup_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    signer: JwtSigner,
    pg_engine: Engine,
) -> None:
    """Every successful push inserts an append-only auth_profile_writes row."""
    t, p, m = setup_tenant
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    # Two pushes with different hashes → two audit rows.
    r1 = client.post(
        "/v1/auth-profiles",
        json=_push_payload(),
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r1.status_code == 200, r1.text
    payload2 = _push_payload()
    payload2["source_file_hash"] = "feedface" * 8
    r2 = client.post(
        "/v1/auth-profiles",
        json=payload2,
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r2.status_code == 200, r2.text

    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        rows = conn.execute(
            text(
                "SELECT source_file_hash, machine_id, auth_profile_id "
                "FROM auth_profile_writes "
                "WHERE tenant_id = :t AND principal_id = :p "
                "ORDER BY written_at ASC"
            ),
            {"t": t, "p": p},
        ).fetchall()
    assert len(rows) == 2
    assert rows[0].source_file_hash == "deadbeef" * 8
    assert rows[1].source_file_hash == "feedface" * 8
    assert all(r.machine_id == m and r.auth_profile_id == "codex/user@example.com" for r in rows)


def test_push_missing_auth(client: TestClient) -> None:
    r = client.post("/v1/auth-profiles", json=_push_payload())
    assert r.status_code == 401


def test_push_rejects_unknown_machine(
    client: TestClient,
    setup_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    signer: JwtSigner,
) -> None:
    """JWT minted for a (tenant, principal) but carrying a machine_id that
    has NO daemon_machines row must 401 with machine_unknown."""
    t, p, _m = setup_tenant
    ghost_machine = uuid.uuid4()
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=ghost_machine),
        ttl=timedelta(hours=1),
    )
    r = client.post(
        "/v1/auth-profiles",
        json=_push_payload(),
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "machine_unknown"


def test_push_rejects_revoked_machine(
    client: TestClient,
    setup_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    signer: JwtSigner,
    pg_engine: Engine,
) -> None:
    """Revoking a machine_id must take effect immediately — the JWT is still
    signature-valid but the push must 401 with machine_revoked."""
    t, p, m = setup_tenant
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        conn.execute(
            text("UPDATE daemon_machines SET revoked_at = NOW() WHERE id = :m AND tenant_id = :t"),
            {"m": str(m), "t": str(t)},
        )
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    r = client.post(
        "/v1/auth-profiles",
        json=_push_payload(),
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "machine_revoked"


def test_push_stale_write_logged_but_accepted(
    client: TestClient,
    setup_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    signer: JwtSigner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    t, p, m = setup_tenant
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    # First write establishes the row (and its updated_at = NOW()).
    r1 = client.post(
        "/v1/auth-profiles",
        json=_push_payload(),
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r1.status_code == 200, r1.text
    # Second write with DIFFERENT hash but EARLIER updated_at_override → conflict log.
    payload = _push_payload()
    payload["source_file_hash"] = "cafef00d" * 8
    payload["updated_at_override"] = "1970-01-01T00:00:00+00:00"
    with caplog.at_level(logging.WARNING):
        r2 = client.post(
            "/v1/auth-profiles",
            json=payload,
            headers={"Authorization": f"Bearer {jwt_str}"},
        )
    assert r2.status_code == 200, r2.text
    assert any("push_conflict_stale_write" in rec.getMessage() for rec in caplog.records)
