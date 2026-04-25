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
    # RFC 6749 §5.1: expires_in is OPTIONAL. Classic GitHub PATs have no
    # expiry — the field must be omitted, not emitted as 0 (which clients
    # interpret as "already expired"). Regression for codex round-5 F16.
    assert "expires_in" not in body


def test_token_exchange_emits_expires_in_when_credential_has_expiry(
    engine: Engine,
) -> None:
    """Fine-grained PATs / STS creds carry expires_at — the wire response
    MUST include expires_in. Companion to the classic-PAT happy path which
    asserts the field is OMITTED. Together they pin RFC 6749 §5.1 semantics.
    """
    from datetime import UTC, datetime

    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)

    # Seed a github profile whose envelope payload carries expires_at.
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    payload = json.dumps(
        {"token": "github_pat_real", "scopes": ["repo"], "expires_at": expires_at.isoformat()}
    ).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-default"
    dek = b"\x03" * 32
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
    assert "expires_in" in body
    # ~30 minutes minus a few seconds of test elapsed time
    assert 1500 <= body["expires_in"] <= 1800


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


def test_parse_resource_accepts_known_provider() -> None:
    """Bare provider URI parses to (provider, None)."""
    from nexus.server.api.v1.routers.token_exchange import _parse_resource

    assert _parse_resource("urn:nexus:provider:github") == ("github", None)
    assert _parse_resource("urn:nexus:provider:aws") == ("aws", None)


def test_parse_resource_rejects_unknown_provider() -> None:
    from nexus.server.api.v1.routers.token_exchange import _parse_resource

    p, err = _parse_resource("urn:nexus:provider:slack")
    assert p is None and err is not None
    p, err = _parse_resource("urn:nexus:provider:")
    assert p is None and err is not None


def test_parse_resource_rejects_non_nexus_uri() -> None:
    """``resource`` URI scheme is fixed; non-Nexus URNs return parse_err."""
    from nexus.server.api.v1.routers.token_exchange import _parse_resource

    p, err = _parse_resource("https://example.com/api")
    assert p is None and err is not None


def test_validate_profile_id_accepts_real_daemon_ids() -> None:
    """Real daemon-written profile IDs use ``<provider>/<account_identifier>``
    where account_identifier may contain ``/``, ``.``, ``@``, etc. The
    validator must accept these verbatim — that's the whole reason F23
    moved profile_id off the resource URI and onto its own form field."""
    from nexus.server.api.v1.routers.token_exchange import _validate_profile_id

    for ok in (
        "github-default",
        "github/github.com/alice",
        "codex/u@example.com",
        "aws/123456789012",
        "github/org-name/sub-org/account",  # deeply nested daemon path
    ):
        assert _validate_profile_id(ok) is None, f"should accept {ok!r}"


def test_validate_profile_id_rejects_empty_oversize_or_control_chars() -> None:
    from nexus.server.api.v1.routers.token_exchange import (
        _PROFILE_ID_MAX_LEN,
        _validate_profile_id,
    )

    assert _validate_profile_id("") is not None
    assert _validate_profile_id("x" * (_PROFILE_ID_MAX_LEN + 1)) is not None
    assert _validate_profile_id("has\x00null") is not None
    assert _validate_profile_id("has\nnewline") is not None
    assert _validate_profile_id("has\x7fdel") is not None


def _seed_github_profile(
    engine: Engine,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    encryption: InMemoryEncryptionProvider,
    *,
    profile_id: str,
    account_identifier: str,
    token: str,
) -> None:
    """Insert one github envelope row with a caller-chosen profile_id.

    Mirrors the daemon's ``Pusher.push_source`` shape — profile_id is
    ``<provider>/<account_identifier>`` and may contain ``/``, ``.``, ``@``.
    """
    payload = json.dumps({"token": token, "scopes": ["repo"]}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + profile_id.encode()
    dek = bytes([profile_id.__hash__() & 0xFF]) * 32  # arbitrary distinct key per row
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
                "VALUES (:t, :p, :pid, 'github', :acct, 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant),
                "p": str(principal),
                "pid": profile_id,
                "acct": account_identifier,
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def test_token_exchange_with_profile_id_picks_named_envelope(engine: Engine) -> None:
    """End-to-end: 2 GitHub envelopes with REAL daemon-style profile IDs;
    the named one via the ``nexus_profile_id`` form field returns its own
    access_token.

    Uses ``github/github.com/alice`` and ``codex/u@example.com`` shapes —
    proves the F23 fix actually addresses production daemon writes (vs
    the synthetic ``github-default`` slugs the original F21 test used).
    """
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant, principal)
    _seed_github_profile(
        engine,
        tenant,
        principal,
        encryption,
        profile_id="github/github.com/alice",
        account_identifier="github.com/alice",
        token="ghp_alice",
    )
    _seed_github_profile(
        engine,
        tenant,
        principal,
        encryption,
        profile_id="github/u@example.com",
        account_identifier="u@example.com",
        token="ghp_email",
    )
    machine = _seed_active_machine(engine, tenant, principal)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)

    base = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": jwt,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "resource": "urn:nexus:provider:github",
        "scope": "x",
    }

    # Bare resource (no profile_id form field) → 409 ambiguous_profile.
    r = client.post("/v1/auth/token-exchange", data=base)
    assert r.status_code == 409
    assert r.json()["error"] == "ambiguous_profile"

    # nexus_profile_id selects the alice account.
    r = client.post(
        "/v1/auth/token-exchange",
        data={**base, "nexus_profile_id": "github/github.com/alice"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == "ghp_alice"

    # nexus_profile_id selects the email-shaped account.
    r = client.post(
        "/v1/auth/token-exchange",
        data={**base, "nexus_profile_id": "github/u@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == "ghp_email"

    # Unknown nexus_profile_id → 403 access_denied (no silent fallback).
    r = client.post(
        "/v1/auth/token-exchange",
        data={**base, "nexus_profile_id": "github/no-such-account"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "access_denied"

    # Empty form field is treated as "not provided" — same path as bare.
    r = client.post("/v1/auth/token-exchange", data={**base, "nexus_profile_id": ""})
    assert r.status_code == 409  # back to ambiguous

    # Control-char-bearing profile_id → 400 invalid_request before resolve runs.
    r = client.post(
        "/v1/auth/token-exchange",
        data={**base, "nexus_profile_id": "has\nnewline"},
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
