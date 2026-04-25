"""Integration tests for read-path additions to PostgresAuthProfileStore (#3818).

Requires a running Postgres (env: NEXUS_TEST_DATABASE_URL). Skip cleanly when absent.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.envelope import (
    AESGCMEnvelope,
    DEKCache,
)
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def test_auth_profile_reads_table_exists(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'auth_profile_reads' ORDER BY ordinal_position"
            )
        ).fetchall()
    cols = [r[0] for r in rows]
    assert cols == [
        "id",
        "read_at",
        "tenant_id",
        "principal_id",
        "auth_profile_id",
        "caller_machine_id",
        "caller_kind",
        "provider",
        "purpose",
        "cache_hit",
        "kek_version",
    ]


def test_auth_profile_reads_has_rls_enabled(engine):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'auth_profile_reads'"
            )
        ).fetchone()
    assert row == (True, True)


# ---------------------------------------------------------------------------
# Task 2: decrypt_profile() helper
# ---------------------------------------------------------------------------


def _seed_envelope_row(
    *,
    engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    profile_id: str,
    provider: str,
    plaintext: bytes,
    encryption,
):
    """Helper: seed a fully-formed envelope row for decrypt tests."""
    aad = str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|" + profile_id.encode()
    dek = b"\x00" * 32  # AES-256 zero key — fine for an in-memory test fake
    nonce, ciphertext = AESGCMEnvelope().encrypt(dek, plaintext, aad=aad)
    wrapped, kek_version = encryption.wrap_dek(dek, tenant_id=tenant_id, aad=aad)
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(tenant_id)},
        )
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id), "n": f"test-{tenant_id}"},
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
                "VALUES (:t, :p, :id, :prov, 'acct', 'envelope', 'k', NOW(), 300, "
                " :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "id": profile_id,
                "prov": provider,
                "ct": ciphertext,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kek_version,
            },
        )


def test_decrypt_profile_returns_plaintext_and_kek_version(engine):
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    plaintext = b'{"token":"ghp_test"}'
    _seed_envelope_row(
        engine=engine,
        tenant_id=tenant_id,
        principal_id=principal_id,
        profile_id="github-default",
        provider="github",
        plaintext=plaintext,
        encryption=encryption,
    )

    # NOTE: The store's __init__ requires a principal_id; the new
    # ``decrypt_profile`` method takes its own ``principal_id`` parameter
    # because in the server-side consumer path one store may resolve
    # credentials for many principals in the tenant. We pass the seeded
    # principal_id at construction so RLS / scoping helpers stay consistent.
    store = PostgresAuthProfileStore(
        "",  # db_url unused when engine is supplied
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=engine,
    )
    out = store.decrypt_profile(
        principal_id=principal_id,
        provider="github",
        encryption=encryption,
        dek_cache=DEKCache(),
    )

    assert out.plaintext == plaintext
    assert out.profile_id == "github-default"
    assert out.kek_version == 1
    assert out.last_synced_at is not None


def test_decrypt_profile_raises_profile_not_found(engine):
    from nexus.bricks.auth.postgres_profile_store import ProfileNotFound

    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    # Seed tenant but no profile
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id), "n": f"tx-{tenant_id}"},
        )

    store = PostgresAuthProfileStore(
        "",  # db_url unused when engine is supplied
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=engine,
    )
    encryption = InMemoryEncryptionProvider()
    with pytest.raises(ProfileNotFound):
        store.decrypt_profile(
            principal_id=principal_id,
            provider="aws",
            encryption=encryption,
            dek_cache=DEKCache(),
        )


def test_decrypted_profile_repr_masks_plaintext(engine):
    """The DecryptedProfile dataclass must NOT print plaintext in its repr.

    A Task-8 caller that ``log.info("decrypted %s", out)`` or hits an
    unhandled exception (locals frame) must not leak the credential JSON.
    """
    from nexus.bricks.auth.postgres_profile_store import DecryptedProfile

    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    plaintext = b'{"token":"super_secret_value"}'
    _seed_envelope_row(
        engine=engine,
        tenant_id=tenant_id,
        principal_id=principal_id,
        profile_id="github-default",
        provider="github",
        plaintext=plaintext,
        encryption=encryption,
    )
    store = PostgresAuthProfileStore(
        "",
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=engine,
    )
    out = store.decrypt_profile(
        principal_id=principal_id,
        provider="github",
        encryption=encryption,
        dek_cache=DEKCache(),
    )
    assert isinstance(out, DecryptedProfile)
    # Sanity: plaintext is recoverable via the field, just not via repr.
    assert out.plaintext == plaintext
    rendered = repr(out)
    assert "super_secret_value" not in rendered
    assert b"super_secret_value" not in rendered.encode()


def test_decrypt_profile_rejects_tampered_aad(engine):
    """Defense-in-depth: a row whose aad column has been tampered with
    (different from what writers compute) must be rejected even if the
    AES-GCM tag verifies under the tampered AAD."""
    from nexus.bricks.auth.envelope import AADMismatch

    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    plaintext = b'{"token":"x"}'
    _seed_envelope_row(
        engine=engine,
        tenant_id=tenant_id,
        principal_id=principal_id,
        profile_id="github-default",
        provider="github",
        plaintext=plaintext,
        encryption=encryption,
    )
    # Tamper: rewrite aad to a different (but well-formed) value, AND
    # re-encrypt with the new AAD so the AES-GCM tag still verifies.
    # The defense-in-depth check should catch this.
    tampered_aad = str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|other-profile"
    dek = b"\x00" * 32
    nonce, new_ct = AESGCMEnvelope().encrypt(dek, plaintext, aad=tampered_aad)
    new_wrapped, _ = encryption.wrap_dek(dek, tenant_id=tenant_id, aad=tampered_aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "UPDATE auth_profiles SET aad = :aad, ciphertext = :ct, "
                "wrapped_dek = :wd, nonce = :no "
                "WHERE tenant_id = :t AND id = 'github-default'"
            ),
            {
                "aad": tampered_aad,
                "ct": new_ct,
                "wd": new_wrapped,
                "no": nonce,
                "t": str(tenant_id),
            },
        )

    store = PostgresAuthProfileStore(
        "",
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=engine,
    )
    with pytest.raises(AADMismatch):
        store.decrypt_profile(
            principal_id=principal_id,
            provider="github",
            encryption=encryption,
            dek_cache=DEKCache(),
        )
