"""E2E: daemon push → /v1/auth/token-exchange → real S3 list-buckets (#3818).

PR-CI variant: uses LocalStack S3 (deterministic, no live AWS).
Nightly variant: set NEXUS_TEST_AWS_LIVE=1 to exercise live STS + S3.

Requires:
  - NEXUS_TEST_DATABASE_URL (Postgres)
  - LOCALSTACK_ENDPOINT (e.g. http://localhost:4566) — falls back to skip
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
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
    if not (os.environ.get("LOCALSTACK_ENDPOINT") or os.environ.get("NEXUS_TEST_AWS_LIVE")):
        pytest.skip("LOCALSTACK_ENDPOINT or NEXUS_TEST_AWS_LIVE required")


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


def test_s3_list_buckets_as_user_via_token_exchange():
    _maybe_skip()
    import boto3

    endpoint = os.environ.get("LOCALSTACK_ENDPOINT")
    using_live = os.environ.get("NEXUS_TEST_AWS_LIVE") == "1"

    # 1. Provision creds — for LocalStack the canonical "test" creds work.
    if using_live:
        aws_creds = {
            "access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
            "secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            "session_token": os.environ.get("AWS_SESSION_TOKEN", ""),
            "expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "region": os.environ.get("AWS_REGION", "us-east-1"),
        }
    else:
        aws_creds = {
            "access_key_id": "test",
            "secret_access_key": "test",
            "session_token": "test",
            "expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "region": "us-east-1",
        }

    # 2. Set up DB + envelope + app stack.
    engine = create_engine(os.environ["NEXUS_TEST_DATABASE_URL"], future=True)
    ensure_schema(engine)
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()

    encryption = InMemoryEncryptionProvider()
    payload = json.dumps(aws_creds).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"aws-default"
    dek = b"\x09" * 32
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
                "VALUES (:t, :p, 'aws-default', 'aws', 'me', 'envelope', 'k', "
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
        adapters={"aws": AwsProviderAdapter()},
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

    # 3. Call token-exchange.
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
            "resource": "urn:nexus:provider:aws",
            "scope": "list-buckets",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    meta = body["nexus_credential_metadata"]

    # 4. Build a real boto3 session and call S3.
    s3_kwargs = {
        "aws_access_key_id": meta["access_key_id"],
        "aws_secret_access_key": meta["secret_access_key"],
        "aws_session_token": body["access_token"],
        "region_name": meta["region"],
    }
    if endpoint:
        s3_kwargs["endpoint_url"] = endpoint

    s3 = boto3.client("s3", **s3_kwargs)
    # In LocalStack: empty bucket list is fine — the call succeeding is the win.
    resp = s3.list_buckets()
    assert "Buckets" in resp

    engine.dispose()
