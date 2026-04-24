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
    MachineUnknownOrRevoked,
    MultipleProfilesForProvider,
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


def _seed_active_machine(*, engine, tenant_id, principal_id) -> uuid.UUID:
    """Insert a tenant/principal/active-daemon row; return the machine UUID.

    Required because consumer.resolve now performs a daemon_machines
    revocation check before any cache lookup or decrypt.
    """
    machine_id = uuid.uuid4()
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
                "INSERT INTO daemon_machines (id, tenant_id, principal_id, pubkey) "
                "VALUES (:m, :t, :p, :pk) ON CONFLICT DO NOTHING"
            ),
            {
                "m": str(machine_id),
                "t": str(tenant_id),
                "p": str(principal_id),
                "pk": b"test-pubkey-" + machine_id.bytes,
            },
        )
    return machine_id


def _revoke_machine(*, engine, tenant_id, machine_id) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("UPDATE daemon_machines SET revoked_at = NOW() WHERE id = :m"),
            {"m": str(machine_id)},
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


def _claims(tenant_id, principal_id, machine_id=None):
    return DaemonClaims(
        tenant_id=tenant_id,
        principal_id=principal_id,
        machine_id=machine_id or uuid.uuid4(),
    )


def test_resolve_happy_path_returns_materialized_cred(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    out = consumer.resolve(
        claims=_claims(tenant, principal, machine),
        provider="github",
        purpose="list-repos",
    )
    assert out.access_token == "ghp_test"
    assert out.metadata["scopes_csv"] == "repo"


def test_resolve_warm_cache_skips_decrypt(engine):
    """Cache hit returns the same materialized object without re-running
    envelope decryption. (Cache hits still re-check profile state via
    assert_profile_active — see test_cache_hit_evicts_when_disabled.)
    """
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    first = consumer.resolve(
        claims=_claims(tenant, principal, machine), provider="github", purpose="x"
    )
    unwraps_after_first = encryption.unwrap_count
    second = consumer.resolve(
        claims=_claims(tenant, principal, machine), provider="github", purpose="x"
    )
    assert encryption.unwrap_count == unwraps_after_first, "second call must not unwrap"
    assert second is first  # cached, same object


def test_resolve_force_refresh_bypasses_cache(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    # Prime the cache.
    consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(text("DELETE FROM auth_profiles WHERE tenant_id = :t"), {"t": str(tenant)})

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(
            claims=_claims(tenant, principal, machine),
            provider="github",
            purpose="x",
            force_refresh=True,
        )


def test_resolve_raises_profile_not_found(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal)

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


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
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(StaleSource):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


def test_resolve_rejects_unknown_machine(engine):
    """JWT-valid but no daemon_machines row → MachineUnknownOrRevoked."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    # No _seed_active_machine — claims carry a fresh machine_id with no row.
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(claims=_claims(tenant, principal), provider="github", purpose="x")
    assert exc.value.cause == "machine_unknown"


def test_resolve_rejects_revoked_machine_even_when_cached(engine):
    """Revoking the daemon row mid-cache must invalidate further reads."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    # Prime cache.
    consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    # Revoke after cache populated.
    _revoke_machine(engine=engine, tenant_id=tenant, machine_id=machine)
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    assert exc.value.cause == "machine_revoked"


def test_audit_failure_evicts_cred_from_cache(engine):
    """A failed cache-miss audit must NOT pollute cred_cache. If it did, the
    next request would hit cache (no audit attempted) and silently return a
    credential with no durable read record — exactly the gap F8 plugged.
    Asserts the order: audit.write happens BEFORE cred_cache.put.
    """
    from unittest.mock import MagicMock

    from nexus.bricks.auth.consumer import AuditWriteFailed

    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)

    bound_principal = principal
    store = PostgresAuthProfileStore(
        "", tenant_id=tenant, principal_id=bound_principal, engine=engine
    )
    cache = ResolvedCredCache(ceiling_seconds=300)
    audit = MagicMock()
    audit.write.side_effect = AuditWriteFailed.from_row(
        tenant_id=tenant,
        principal_id=principal,
        provider="github",
        cause="forced",
    )
    consumer = CredentialConsumer(
        store=store,
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=cache,
        adapters={"github": GithubProviderAdapter()},
        audit=audit,
    )

    with pytest.raises(AuditWriteFailed):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")

    # Cache must NOT contain the materialized cred — second call must
    # re-attempt audit (and fail again), NOT silently return from cache.
    audit.write.reset_mock()
    audit.write.side_effect = AuditWriteFailed.from_row(
        tenant_id=tenant,
        principal_id=principal,
        provider="github",
        cause="still failing",
    )
    with pytest.raises(AuditWriteFailed):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    assert audit.write.call_count == 1, "second resolve must re-attempt audit, not hit cache"


def test_resolve_skips_disabled_profile(engine):
    """A profile with disabled_until in the future must not be returned."""

    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET disabled_until = NOW() + INTERVAL '1 hour' "
                "WHERE tenant_id = :t"
            ),
            {"t": str(tenant)},
        )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


def test_resolve_skips_cooled_down_profile(engine):
    """A profile with cooldown_until in the future must not be returned."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET cooldown_until = NOW() + INTERVAL '5 minutes' "
                "WHERE tenant_id = :t"
            ),
            {"t": str(tenant)},
        )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


def _setup_cross_machine_envelope(engine, encryption):
    """Helper: envelope pushed by daemon X, reader is daemon Y on same principal."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    writer_machine = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "INSERT INTO daemon_machines (id, tenant_id, principal_id, pubkey) "
                "VALUES (:m, :t, :p, :pk) ON CONFLICT DO NOTHING"
            ),
            {
                "m": str(writer_machine),
                "t": str(tenant),
                "p": str(principal),
                "pk": b"writer-pk-" + writer_machine.bytes,
            },
        )
        conn.execute(
            text("UPDATE auth_profiles SET machine_id = :m WHERE tenant_id = :t"),
            {"m": str(writer_machine), "t": str(tenant)},
        )
    reader_machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    return tenant, principal, reader_machine, writer_machine


def test_resolve_rejects_cross_machine_read_by_default(engine, monkeypatch):
    """Default: reader.machine_id != writer.machine_id → MachineUnknownOrRevoked.
    Prevents a compromised secondary daemon from exchanging another
    machine's pushed credential."""
    monkeypatch.delenv("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", raising=False)
    encryption = InMemoryEncryptionProvider()
    tenant, principal, reader_machine, _ = _setup_cross_machine_envelope(engine, encryption)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(
            claims=_claims(tenant, principal, reader_machine), provider="github", purpose="x"
        )
    assert exc.value.cause == "cross_machine_read_disallowed"


def test_resolve_allows_cross_machine_read_when_opted_in(engine, monkeypatch, caplog):
    """Opt-in via env: cross-machine read succeeds; warning + metric still fire."""
    import logging

    monkeypatch.setenv("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", "1")
    encryption = InMemoryEncryptionProvider()
    tenant, principal, reader_machine, _ = _setup_cross_machine_envelope(engine, encryption)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with caplog.at_level(logging.WARNING, logger="nexus.bricks.auth.consumer"):
        out = consumer.resolve(
            claims=_claims(tenant, principal, reader_machine), provider="github", purpose="x"
        )
    assert out.access_token == "ghp_test"
    assert any("cross_machine_read" in rec.message for rec in caplog.records)


def test_cache_hit_evicts_when_profile_disabled_after_caching(engine):
    """A profile disabled AFTER cache prime must take effect within ms, not
    wait for the cache TTL."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    # Operator disables the profile while the cache is still warm.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET disabled_until = NOW() + INTERVAL '1 hour' "
                "WHERE tenant_id = :t"
            ),
            {"t": str(tenant)},
        )
    with pytest.raises(ProfileNotFoundForCaller) as exc:
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    assert exc.value.cause == "profile_disabled_or_cooldown"


def test_resolve_rejects_when_multiple_profiles_for_provider(engine):
    """Two envelope rows for same (tenant, principal, provider) → fail closed."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    # First profile — uses the helper which writes 'github-default'.
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    # Second profile, different id, same provider — manually inserted.
    payload = json.dumps({"token": "ghp_other", "scopes": ["repo"]}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-other"
    dek = b"\x02" * 32
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
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(MultipleProfilesForProvider):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
