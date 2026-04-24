"""Tests for /v1/auth/token-exchange router (#3818)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router


def _make_signer() -> JwtSigner:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pk = ec.generate_private_key(ec.SECP256R1())
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return JwtSigner.from_pem(pem, issuer="https://test.local")


@pytest.fixture
def engine() -> Iterator[Engine]:
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _build_app(
    engine: Engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID | None = None,
) -> tuple[FastAPI, JwtSigner, InMemoryEncryptionProvider]:
    encryption = InMemoryEncryptionProvider()
    signer = _make_signer()
    bound_principal = principal_id or uuid.uuid4()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore(
            "", tenant_id=tenant_id, principal_id=bound_principal, engine=engine
        ),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={
            "aws": AwsProviderAdapter(),
            "github": GithubProviderAdapter(),
        },
        audit=ReadAuditWriter(engine=engine, hit_sample_rate=0.01),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=True,
            signer=signer,
            consumer=consumer,
            encryption=encryption,
        )
    )
    return app, signer, encryption


def _seed_github(
    engine: Engine,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    encryption: InMemoryEncryptionProvider,
) -> None:
    payload = json.dumps({"token": "ghp_real", "scopes": ["repo"]}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-default"
    dek = b"\x02" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant), "n": f"tx-{tenant}"},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal), "t": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-default', 'github', 'me', 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant),
                "p": str(principal),
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def _seed_active_machine(engine: Engine, tenant: uuid.UUID, principal: uuid.UUID) -> uuid.UUID:
    machine_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant), "n": f"tx-{tenant}"},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal), "t": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO daemon_machines (id, tenant_id, principal_id, pubkey) "
                "VALUES (:m, :t, :p, :pk) ON CONFLICT DO NOTHING"
            ),
            {
                "m": str(machine_id),
                "t": str(tenant),
                "p": str(principal),
                "pk": b"test-pubkey-" + machine_id.bytes,
            },
        )
    return machine_id


def test_token_exchange_happy_path_returns_200_with_bearer(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)
    _seed_github(engine, tenant, principal, encryption)
    machine = _seed_active_machine(engine, tenant, principal)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )

    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "list-repos",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "ghp_real"
    assert body["token_type"] == "Bearer"
    assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert "nexus_credential_metadata" in body
    assert body["nexus_credential_metadata"]["scopes_csv"] == "repo"
    # RFC 6749 §5.1: token responses MUST NOT be cached.
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("pragma") == "no-cache"


def test_token_exchange_revoked_machine_returns_401(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)
    _seed_github(engine, tenant, principal, encryption)
    machine = _seed_active_machine(engine, tenant, principal)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("UPDATE daemon_machines SET revoked_at = NOW() WHERE id = :m"),
            {"m": str(machine)},
        )
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert "machine_revoked" in r.json()["error_description"]


def test_token_exchange_unknown_machine_returns_401(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)
    _seed_github(engine, tenant, principal, encryption)
    # Don't seed daemon_machines — JWT carries an unknown machine_id.
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=uuid.uuid4()),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert "machine_unknown" in r.json()["error_description"]


def test_token_exchange_invalid_jwt_returns_401(engine: Engine) -> None:
    tenant = uuid.uuid4()
    app, _, _ = _build_app(engine, tenant)
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "garbage",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"


def test_token_exchange_unknown_resource_returns_400(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, _ = _build_app(engine, tenant, principal)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=uuid.uuid4()),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:slack",  # unknown
            "scope": "x",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_exchange_no_profile_returns_403(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, _ = _build_app(engine, tenant, principal)
    # Seed tenant + principal + active machine but no envelope profile
    machine = _seed_active_machine(engine, tenant, principal)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"] == "access_denied"


def test_token_exchange_audit_write_failed_returns_503() -> None:
    """Cache-miss audit insert failure must surface as 503, not silently
    return the credential without an audit row."""
    from unittest.mock import MagicMock

    from nexus.bricks.auth.consumer import AuditWriteFailed

    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    signer = _make_signer()
    fake_consumer = MagicMock()
    fake_consumer.resolve.side_effect = AuditWriteFailed.from_row(
        tenant_id=tenant,
        principal_id=principal,
        provider="github",
        cause="OperationalError",
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=True,
            signer=signer,
            consumer=fake_consumer,
            encryption=encryption,
        )
    )
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 503
    assert r.json()["error"] == "audit_unavailable"
    assert r.headers.get("cache-control") == "no-store"


def test_token_exchange_multiple_profiles_returns_409(engine: Engine) -> None:
    """Two envelope rows for the same (tenant, principal, provider) → 409."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)
    _seed_github(engine, tenant, principal, encryption)
    # Insert second profile under a different id but same provider.
    payload = json.dumps({"token": "ghp_other", "scopes": []}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-other"
    dek = b"\x05" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-other', 'github', 'other', 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant),
                "p": str(principal),
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )
    machine = _seed_active_machine(engine, tenant, principal)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"] == "ambiguous_profile"


def test_token_exchange_disabled_returns_501(engine: Engine) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    signer = _make_signer()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore("", tenant_id=tenant, principal_id=principal, engine=engine),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=False,
            signer=signer,
            consumer=consumer,
            encryption=encryption,
        )
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "x",
            "subject_token_type": "x",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 501
