"""Tests for CredentialConsumer.resolve — orchestrator covering happy / cache /
stale / errors (#3818)."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import (
    CredentialConsumer,
    ProfileNotFoundForCaller,
    ProviderNotConfigured,
    StaleSource,
)
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _seed_github_envelope(
    *, engine, tenant_id, principal_id, encryption, sync_ttl=300, lsa_offset_seconds=0
):
    """Seed a github profile with a pushed envelope."""
    payload = json.dumps({"token": "ghp_test", "scopes": ["repo"]}).encode()
    aad = str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|" + b"github-default"
    dek = b"\x01" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant_id, aad=aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id), "n": f"tx-{tenant_id}"},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal_id), "t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-default', 'github', 'me', 'envelope', 'k', "
                " NOW() - (:off || ' seconds')::INTERVAL, :ttl, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "off": str(lsa_offset_seconds),
                "ttl": sync_ttl,
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def _make_consumer(engine, tenant_id, principal_id=None, encryption=None, cache=None):
    encryption = encryption or InMemoryEncryptionProvider()
    cache = cache or ResolvedCredCache(ceiling_seconds=300)
    # PostgresAuthProfileStore requires db_url (empty when engine is provided) and principal_id
    # The store's bound principal_id is unused by decrypt_profile (which takes its own principal_id arg).
    bound_principal = principal_id or uuid.uuid4()
    store = PostgresAuthProfileStore(
        "", tenant_id=tenant_id, principal_id=bound_principal, engine=engine
    )
    return CredentialConsumer(
        store=store,
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=cache,
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine, hit_sample_rate=0.01),
    )


def _claims(tenant_id, principal_id):
    return DaemonClaims(
        tenant_id=tenant_id,
        principal_id=principal_id,
        machine_id=uuid.uuid4(),
    )


def test_resolve_happy_path_returns_materialized_cred(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    out = consumer.resolve(
        claims=_claims(tenant, principal),
        provider="github",
        purpose="list-repos",
    )
    assert out.access_token == "ghp_test"
    assert out.metadata["scopes_csv"] == "repo"


def test_resolve_warm_cache_skips_decrypt(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    first = consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")
    # Drop the row so a second decrypt would fail
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(text("DELETE FROM auth_profiles WHERE tenant_id = :t"), {"t": str(tenant)})

    second = consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")
    assert second is first  # cached, same object


def test_resolve_force_refresh_bypasses_cache(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    # Prime the cache.
    consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(text("DELETE FROM auth_profiles WHERE tenant_id = :t"), {"t": str(tenant)})

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(
            claims=_claims(tenant, principal),
            provider="github",
            purpose="x",
            force_refresh=True,
        )


def test_resolve_raises_profile_not_found(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant), "n": f"tx-{tenant}"},
        )
    consumer = _make_consumer(engine, tenant, principal)

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")


def test_resolve_raises_provider_not_configured(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    consumer = _make_consumer(engine, tenant, principal)
    with pytest.raises(ProviderNotConfigured):
        consumer.resolve(
            claims=_claims(tenant, principal),
            provider="unknown",
            purpose="x",
        )


def test_resolve_raises_stale_source_when_last_synced_past_ttl(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        sync_ttl=60,
        lsa_offset_seconds=120,  # 2 minutes ago, TTL is 60s → stale
    )
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(StaleSource):
        consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")
