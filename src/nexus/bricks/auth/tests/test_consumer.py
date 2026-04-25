"""Tests for CredentialConsumer.resolve — orchestrator covering happy / cache /
stale / errors (#3818)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

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


def test_warm_cache_evicts_when_source_goes_stale(engine):
    """Daemon primes cache, then stops pushing past sync_ttl. Next read on the
    warm cache must surface StaleSource (and evict) instead of returning
    the cached plaintext for the rest of the cache TTL.

    Regression for codex round-5 finding F15.
    """
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
    # Push the row's last_synced_at past sync_ttl_seconds (default 300s in helper).
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET last_synced_at = NOW() - INTERVAL '1 hour' "
                "WHERE tenant_id = :t"
            ),
            {"t": str(tenant)},
        )
    with pytest.raises(StaleSource):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


def test_cross_machine_denial_does_not_decrypt_payload(engine, monkeypatch):
    """Disallowed cross-machine read must reject BEFORE the envelope is
    unwrapped — we never want another daemon's plaintext to land in
    process memory just to be discarded.

    Counts ``encryption.unwrap_count`` and proves it stays at 0 across
    a denied request. Regression for codex round-7 finding F22.
    """
    monkeypatch.delenv("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", raising=False)
    encryption = InMemoryEncryptionProvider()
    tenant, principal, reader_machine, _ = _setup_cross_machine_envelope(engine, encryption)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    before = encryption.unwrap_count
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(
            claims=_claims(tenant, principal, reader_machine), provider="github", purpose="x"
        )
    assert exc.value.cause == "cross_machine_read_disallowed"
    assert encryption.unwrap_count == before, (
        "DEK was unwrapped despite the cross-machine policy denying the read"
    )


def test_stale_source_does_not_decrypt_payload(engine):
    """Stale-source rejection on cache miss must reject BEFORE decrypt —
    a stuck daemon's last envelope must never be materialized just to
    return 409 stale_source. Regression for codex round-7 finding F22.
    """
    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    # Seed the row with last_synced_at already past sync_ttl_seconds (300).
    _seed_github_envelope(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        lsa_offset_seconds=3600,  # one hour stale
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    before = encryption.unwrap_count
    with pytest.raises(StaleSource):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    assert encryption.unwrap_count == before, (
        "DEK was unwrapped despite the row being known stale before decrypt"
    )


def test_cache_hit_audit_row_names_real_profile_and_kek_version(engine):
    """Sampled cache-hit audit rows must name the credential that was served
    (profile_id + kek_version from the cached fingerprint), not a sentinel
    ``'cached'`` / ``0`` that breaks incident review and rotation forensics.

    Regression for codex round-6 finding F20.
    """
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    # Force every cache-hit to be sampled so we can assert on the row.
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore("", tenant_id=tenant, principal_id=principal, engine=engine),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(ceiling_seconds=300),
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine, hit_sample_rate=1.0),
    )
    # Prime cache (records cache-miss audit) then re-read (records sampled hit).
    consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        rows = list(
            conn.execute(
                text(
                    "SELECT auth_profile_id, kek_version, cache_hit "
                    "FROM auth_profile_reads WHERE tenant_id = :t "
                    "ORDER BY read_at"
                ),
                {"t": str(tenant)},
            )
        )
    miss = next(r for r in rows if r.cache_hit is False)
    hit = next(r for r in rows if r.cache_hit is True)
    # Both rows name the same real credential; the cache-hit row no longer
    # uses sentinel placeholders.
    assert hit.auth_profile_id == miss.auth_profile_id == "github-default"
    assert hit.kek_version == miss.kek_version
    assert hit.kek_version > 0


def test_warm_cache_evicts_when_row_rewritten_by_other_machine(engine, monkeypatch):
    """Daemon A primes the cache. Daemon B then overwrites the row's
    machine_id (a fresh push from B for the same principal/provider). A's
    next request must NOT return its cached plaintext: the cache entry's
    fingerprint no longer matches the row, so the consumer evicts and
    re-runs the decrypt path — which then re-applies the cross-machine
    read policy against B as the new writer.

    Without this, A keeps serving its cached plaintext for the rest of the
    cache TTL even after the row was replaced. Regression for codex round-6
    finding F19.
    """
    monkeypatch.delenv("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", raising=False)
    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine_a = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    machine_b = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    # Stamp the existing row with A as the writer so A's first read is in-machine.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("UPDATE auth_profiles SET machine_id = :m WHERE tenant_id = :t"),
            {"m": str(machine_a), "t": str(tenant)},
        )

    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    consumer.resolve(claims=_claims(tenant, principal, machine_a), provider="github", purpose="x")
    # B overwrites the row's writer machine_id AND bumps last_synced_at to
    # simulate "B re-pushed the same provider for the same principal".
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET machine_id = :m, last_synced_at = NOW() "
                "WHERE tenant_id = :t"
            ),
            {"m": str(machine_b), "t": str(tenant)},
        )
    # A's next request: cache key still matches, but fingerprint differs →
    # evict + decrypt path → cross-machine check rejects (writer is now B).
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(
            claims=_claims(tenant, principal, machine_a), provider="github", purpose="x"
        )
    assert exc.value.cause == "cross_machine_read_disallowed"


def test_warm_cache_does_not_bypass_cross_machine_check(engine, monkeypatch):
    """Daemon A primes the cache; Daemon B (different machine_id) must NOT
    inherit A's plaintext from the warm cache.

    Without machine_id in the cache key, B passes assert_machine_active and
    assert_profile_active, then returns A's cached credential — silently
    skipping the writer_machine_id check on the decrypt path. Regression for
    codex round-5 finding F14.
    """
    monkeypatch.delenv("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", raising=False)
    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine_a = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    # Stamp the envelope row's writer_machine_id to A so A's read is in-machine.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("UPDATE auth_profiles SET machine_id = :m WHERE tenant_id = :t"),
            {"m": str(machine_a), "t": str(tenant)},
        )
    machine_b = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)

    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    # Prime the cache as daemon A — happy path, in-machine read.
    consumer.resolve(claims=_claims(tenant, principal, machine_a), provider="github", purpose="x")
    # Daemon B requests with the cache hot — must NOT be served from cache;
    # decrypt path must fire and the cross-machine check must reject.
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        consumer.resolve(
            claims=_claims(tenant, principal, machine_b), provider="github", purpose="x"
        )
    assert exc.value.cause == "cross_machine_read_disallowed"


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
    # Unified cause name — assert_profile_active runs at the front of every
    # request now, raising ProfileNotFound for disable/cooldown/missing alike.
    assert exc.value.cause == "no_active_profile"


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


def _seed_github_envelope_with_expiry(*, engine, tenant_id, principal_id, encryption, expires_at):
    """Seed a github profile whose materialized credential carries an
    explicit expires_at — used to drive F24 (expired-cred rejection)."""
    payload = json.dumps(
        {"token": "ghp_expired", "scopes": ["repo"], "expires_at": expires_at.isoformat()}
    ).encode()
    aad = str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|" + b"github-default"
    dek = b"\x07" * 32
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
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def test_resolve_rejects_expired_materialized_credential(engine):
    """A decrypted PAT/STS payload whose own expires_at is already past
    must surface as StaleSource, not a 200 with ``expires_in: 0``.

    Regression for codex round-9 finding F24.
    """
    from datetime import UTC, datetime

    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope_with_expiry(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(StaleSource) as exc:
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")
    assert "expired" in (exc.value.cause or "")


def test_resolve_rejects_credential_inside_refresh_headroom(engine):
    """Tokens whose expiry is within the 60s refresh-headroom window are
    treated as already-expired — clients shouldn't get a token they have
    less than a minute to use before the upstream provider rejects it."""
    from datetime import UTC, datetime

    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope_with_expiry(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),  # inside headroom
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(StaleSource):
        consumer.resolve(claims=_claims(tenant, principal, machine), provider="github", purpose="x")


def test_resolve_rejects_revoke_committed_after_initial_machine_check(engine):
    """A revoke that commits AFTER the top-of-resolve assert_machine_active
    but BEFORE the response must still be observed and reject the call.

    Approach: monkey-patch ``decrypt_profile`` so that the moment it runs
    (between the two assert_machine_active calls) we revoke the daemon
    out-of-band. The second assert (F25's double-check) must catch the
    revocation and raise MachineUnknownOrRevoked.

    Regression for codex round-9 finding F25.
    """
    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    store = consumer._store  # noqa: SLF001 — test introspection
    real_decrypt = store.decrypt_profile

    def _decrypt_then_revoke(*args, **kwargs):
        result = real_decrypt(*args, **kwargs)
        # Out-of-band revocation in a SEPARATE engine connection so we
        # commit while the resolve call is mid-flight.
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
            conn.execute(
                text("UPDATE daemon_machines SET revoked_at = NOW() WHERE id = :m"),
                {"m": str(machine)},
            )
        return result

    store.decrypt_profile = _decrypt_then_revoke
    try:
        with pytest.raises(MachineUnknownOrRevoked) as exc:
            consumer.resolve(
                claims=_claims(tenant, principal, machine), provider="github", purpose="x"
            )
        assert exc.value.cause == "machine_revoked"
    finally:
        store.decrypt_profile = real_decrypt


def test_audit_write_atomically_rejects_revoked_machine(engine):
    """ReadAuditWriter.write itself enforces the revocation gate inside
    its own transaction (SELECT FOR SHARE on daemon_machines + audit
    INSERT, atomic). Direct test against the writer proves a revoke
    committed BEFORE audit.write starts → MachineUnknownOrRevoked, and
    no audit row gets written.

    The previous regression test wrapped audit.write to commit a revoke
    AFTER it returned; that race-window scenario is now impossible by
    construction (SHARE lock means concurrent revoke blocks until our tx
    commits). This direct-against-writer test pins the new atomic
    semantic. F26 closure.
    """
    from nexus.bricks.auth.read_audit import ReadAuditWriter

    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    # Revoke before any audit attempt.
    _revoke_machine(engine=engine, tenant_id=tenant, machine_id=machine)

    writer = ReadAuditWriter(engine=engine, hit_sample_rate=1.0)
    with pytest.raises(MachineUnknownOrRevoked) as exc:
        writer.write(
            tenant_id=tenant,
            principal_id=principal,
            auth_profile_id="github-default",
            caller_machine_id=machine,
            caller_kind="daemon",
            provider="github",
            purpose="x",
            cache_hit=False,
            kek_version=1,
        )
    assert exc.value.cause == "machine_revoked"

    # No audit row written for the revoked attempt.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM auth_profile_reads "
                "WHERE tenant_id = :t AND caller_machine_id = :m"
            ),
            {"t": str(tenant), "m": str(machine)},
        ).scalar_one()
    assert count == 0


def test_resolve_succeeds_with_active_plus_stale_sibling_profile(engine):
    """A single active default profile + a stale sibling row must NOT
    trigger 409 ambiguous_profile. assert_profile_active filters out the
    stale one, but decrypt_profile used to re-query without that filter
    and see both rows. F27: decrypt is now scoped to the precheck's
    matched profile_id so stale siblings can't widen the query.
    """
    encryption = InMemoryEncryptionProvider()
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    # Active row pushed fresh.
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    # Sibling row, same provider, last_synced_at past sync_ttl_seconds (300).
    payload = json.dumps({"token": "ghp_stale", "scopes": ["repo"]}).encode()
    aad = str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-stale"
    dek = b"\x09" * 32
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
                "VALUES (:t, :p, 'github-stale', 'github', 'stale', 'envelope', 'k', "
                " NOW() - INTERVAL '1 hour', 300, :ct, :wd, :no, :aad, :kv)"
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
    # No profile_id → precheck filters to the one active row, decrypt scoped
    # to that row's id, no ambiguity even though a stale sibling exists.
    out = consumer.resolve(
        claims=_claims(tenant, principal, machine), provider="github", purpose="x"
    )
    assert out.access_token == "ghp_test"


def _seed_named_github_profile(
    *, engine, tenant_id, principal_id, encryption, profile_id, account_identifier, token
):
    """Insert a github envelope row with caller-chosen profile_id (F23).

    Mirrors the daemon's ``Pusher.push_source`` shape — profile_id is
    ``<provider>/<account_identifier>`` and may contain ``/``, ``.``, ``@``.
    """
    payload = json.dumps({"token": token, "scopes": ["repo"]}).encode()
    aad = str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|" + profile_id.encode()
    dek = bytes([profile_id.__hash__() & 0xFF]) * 32
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
                "VALUES (:t, :p, :pid, 'github', :acct, 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "pid": profile_id,
                "acct": account_identifier,
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def _seed_two_real_daemon_envelopes(engine, encryption):
    """Two github profiles with REAL daemon-style IDs containing /, ., @.
    Returns (tenant, principal, machine, alice_pid, email_pid)."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    alice_pid = "github/github.com/alice"
    email_pid = "github/u@example.com"
    _seed_named_github_profile(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        profile_id=alice_pid,
        account_identifier="github.com/alice",
        token="ghp_alice",
    )
    _seed_named_github_profile(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        profile_id=email_pid,
        account_identifier="u@example.com",
        token="ghp_email",
    )
    machine = _seed_active_machine(engine=engine, tenant_id=tenant, principal_id=principal)
    return tenant, principal, machine, alice_pid, email_pid


def test_resolve_with_explicit_profile_id_picks_named_row(engine):
    """Two profiles for one provider with daemon-style IDs (containing
    ``/``, ``.``, ``@``); explicit profile_id selects the right one.

    Regression for codex round-7 F21 + round-8 F23: proves multi-account
    disambiguation works against profile IDs the real daemon writer
    actually produces, not just synthetic single-segment slugs.
    """
    encryption = InMemoryEncryptionProvider()
    tenant, principal, machine, alice_pid, email_pid = _seed_two_real_daemon_envelopes(
        engine, encryption
    )
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)

    alice = consumer.resolve(
        claims=_claims(tenant, principal, machine),
        provider="github",
        profile_id=alice_pid,
        purpose="x",
    )
    email = consumer.resolve(
        claims=_claims(tenant, principal, machine),
        provider="github",
        profile_id=email_pid,
        purpose="x",
    )
    assert alice.access_token == "ghp_alice"
    assert email.access_token == "ghp_email"


def test_resolve_two_profiles_without_profile_id_still_ambiguous(engine):
    """Without profile_id (legacy form), two rows still yield 409. The new
    selector is opt-in — back-compat for single-profile callers is preserved
    by failing closed when the contract is under-specified."""
    encryption = InMemoryEncryptionProvider()
    tenant, principal, machine, _, _ = _seed_two_real_daemon_envelopes(engine, encryption)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(MultipleProfilesForProvider):
        consumer.resolve(
            claims=_claims(tenant, principal, machine),
            provider="github",
            purpose="x",
        )


def test_resolve_with_unknown_profile_id_returns_not_found(engine):
    """Explicit profile_id that matches no row → ProfileNotFoundForCaller.

    Critical: must NOT silently fall back to another profile (which would
    re-introduce the multi-account leak this fix exists to prevent)."""
    encryption = InMemoryEncryptionProvider()
    tenant, principal, machine, _, _ = _seed_two_real_daemon_envelopes(engine, encryption)
    consumer = _make_consumer(engine, tenant, principal, encryption=encryption)
    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(
            claims=_claims(tenant, principal, machine),
            provider="github",
            profile_id="github/nonexistent",
            purpose="x",
        )
