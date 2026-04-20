"""Daemon security regression (#3804)."""

from __future__ import annotations

import os
import stat
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.daemon.keystore import generate_keypair
from nexus.bricks.auth.postgres_profile_store import ensure_principal, ensure_tenant
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.auth_profiles import make_auth_profiles_router

ENROLL_SECRET = b"sec-regression-32bytes-abcdef0123"


@pytest.fixture
def signer() -> JwtSigner:
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return JwtSigner.from_pem(pem, issuer="https://test.nexus")


def test_keyfile_permissions_0600(tmp_path: Path) -> None:
    """Ed25519 keystore MUST write keyfile with mode 0600."""
    key_path = tmp_path / "machine.key"
    generate_keypair(key_path)
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600


def test_push_without_audit_stamps_is_rejected(pg_engine: Engine, signer: JwtSigner) -> None:
    """Server MUST require source_file_hash on push. Missing -> 422."""
    t = ensure_tenant(pg_engine, f"sec-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine, tenant_id=t, external_sub=f"u-{uuid.uuid4()}", auth_method="oidc"
    )
    m = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        conn.execute(
            text(
                "INSERT INTO daemon_machines "
                "(id, tenant_id, principal_id, pubkey, daemon_version_last_seen, "
                " enrolled_at, last_seen_at) "
                "VALUES (:id, :tid, :pid, :pk, :ver, NOW(), NOW())"
            ),
            {"id": str(m), "tid": str(t), "pid": str(p), "pk": b"\x00" * 32, "ver": "0.9.20"},
        )
    app = FastAPI()
    app.include_router(make_auth_profiles_router(engine=pg_engine, signer=signer))
    client = TestClient(app)
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    r = client.post(
        "/v1/auth-profiles",
        json={
            "id": "codex/u@x",
            "provider": "codex",
            "account_identifier": "u@x",
            "backend": "nexus-daemon",
            "backend_key": "codex",
            "envelope": {
                "ciphertext_b64": "AA==",
                "wrapped_dek_b64": "AA==",
                "nonce_b64": "AA==",
                "aad_b64": "AA==",
                "kek_version": 1,
            },
            # source_file_hash intentionally omitted
        },
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r.status_code == 422


def test_rls_blocks_cross_tenant_reads(pg_engine: Engine, signer: JwtSigner) -> None:
    """A JWT scoped to tenant A must not expose any tenant B rows.

    Push with tenant-A JWT -> row lands under tenant A. Switching RLS to tenant B
    must show zero rows. Uses a non-superuser role to avoid BYPASSRLS masking
    the result (mirrors test_daemon_machines_rls_enforced in test_postgres_profile_store.py).
    """
    import base64

    t1 = ensure_tenant(pg_engine, f"rls-a-{uuid.uuid4()}")
    t2 = ensure_tenant(pg_engine, f"rls-b-{uuid.uuid4()}")
    p1 = ensure_principal(
        pg_engine, tenant_id=t1, external_sub=f"u-{uuid.uuid4()}", auth_method="oidc"
    )
    m1 = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t1)})
        conn.execute(
            text(
                "INSERT INTO daemon_machines "
                "(id, tenant_id, principal_id, pubkey, daemon_version_last_seen, "
                " enrolled_at, last_seen_at) "
                "VALUES (:id, :tid, :pid, :pk, :ver, NOW(), NOW())"
            ),
            {"id": str(m1), "tid": str(t1), "pid": str(p1), "pk": b"\x00" * 32, "ver": "0.9.20"},
        )

    app = FastAPI()
    app.include_router(make_auth_profiles_router(engine=pg_engine, signer=signer))
    client = TestClient(app)
    jwt_str = signer.sign(
        DaemonClaims(tenant_id=t1, principal_id=p1, machine_id=m1),
        ttl=timedelta(hours=1),
    )
    r = client.post(
        "/v1/auth-profiles",
        json={
            "id": "codex/u@x",
            "provider": "codex",
            "account_identifier": "u@x",
            "backend": "nexus-daemon",
            "backend_key": "codex",
            "envelope": {
                "ciphertext_b64": base64.b64encode(b"\x01" * 32).decode(),
                "wrapped_dek_b64": base64.b64encode(b"\x02" * 48).decode(),
                "nonce_b64": base64.b64encode(b"\x03" * 12).decode(),
                "aad_b64": base64.b64encode(b"\x04" * 16).decode(),
                "kek_version": 1,
            },
            "source_file_hash": "z" * 64,
        },
        headers={"Authorization": f"Bearer {jwt_str}"},
    )
    assert r.status_code == 200, r.text

    # Tenant B must see zero rows for this profile id under RLS.
    # Use the non-superuser role so RLS is actually enforced.
    with pg_engine.begin() as conn:
        # Ensure the non-superuser role exists (idempotent; mirrors
        # pg_engine fixture's pattern in test_postgres_profile_store.py).
        conn.execute(
            text(
                "DO $$ BEGIN "
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_test_nonsuper') THEN "
                "    CREATE ROLE nexus_test_nonsuper NOSUPERUSER NOBYPASSRLS; "
                "  END IF; "
                "END $$;"
            )
        )
        conn.execute(text("GRANT SELECT ON auth_profiles TO nexus_test_nonsuper"))

    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL SESSION AUTHORIZATION nexus_test_nonsuper"))
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t2)})
        count = conn.execute(
            text("SELECT COUNT(*) FROM auth_profiles WHERE id = :id"),
            {"id": "codex/u@x"},
        ).scalar()
    assert count == 0, "RLS leaked tenant-A row to tenant-B context"
