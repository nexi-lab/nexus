"""E2E: daemon push → /v1/auth/token-exchange → real GitHub /user (#3818).

Requires:
  - NEXUS_TEST_DATABASE_URL (Postgres)
  - NEXUS_TEST_GITHUB_PAT (a real PAT with read:user) — falls back to skip.

The PAT is the "daemon-pushed" credential. We push it, exchange it, and
prove the returned token authenticates against GitHub's /user endpoint.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
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


def _maybe_skip():
    if not os.environ.get("NEXUS_TEST_DATABASE_URL"):
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    if not os.environ.get("NEXUS_TEST_GITHUB_PAT"):
        pytest.skip("NEXUS_TEST_GITHUB_PAT not set")


def _make_signer():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pk = ec.generate_private_key(ec.SECP256R1())
    return JwtSigner.from_pem(
        pk.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        issuer="https://e2e.local",
    )


def test_github_user_endpoint_as_user_via_token_exchange():
    _maybe_skip()

    pat = os.environ["NEXUS_TEST_GITHUB_PAT"]
    engine = create_engine(os.environ["NEXUS_TEST_DATABASE_URL"], future=True)
    ensure_schema(engine)
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()

    encryption = InMemoryEncryptionProvider()
    payload = json.dumps({"token": pat, "scopes": ["read:user"], "token_type": "classic"}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-default"
    dek = b"\x0a" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant), "n": f"e2e-{tenant}"},
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
            enabled=True,
            signer=signer,
            consumer=consumer,
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
            "scope": "get-user",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    bearer = body["access_token"]

    # Real GitHub call.
    gh = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {bearer}", "User-Agent": "nexus-e2e"},
        timeout=10.0,
    )
    assert gh.status_code == 200, gh.text
    assert "login" in gh.json()

    engine.dispose()
