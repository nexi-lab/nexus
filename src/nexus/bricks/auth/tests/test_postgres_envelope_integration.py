"""Integration tests for envelope encryption on PostgresAuthProfileStore (#3803).

Postgres-gated; uses the same TEST_POSTGRES_URL + xdist_group shape as
test_postgres_profile_store.py.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope import AADMismatch, WrappedDEKInvalid
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.tests.conftest import make_profile

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    try:
        eng = create_engine(PG_URL)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason=(
            "PostgreSQL not reachable at TEST_POSTGRES_URL. "
            "Start with: docker compose -f dockerfiles/compose.yaml up postgres -d"
        ),
    ),
]


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    engine = create_engine(PG_URL, future=True)
    drop_schema(engine)
    ensure_schema(engine)
    yield engine
    drop_schema(engine)
    engine.dispose()


@pytest.fixture()
def tenant_id(pg_engine: Engine) -> uuid.UUID:
    return ensure_tenant(pg_engine, f"env-tenant-{uuid.uuid4()}")


@pytest.fixture()
def principal_id(pg_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    return ensure_principal(
        pg_engine,
        tenant_id=tenant_id,
        kind="human",
        external_sub=f"sub-{uuid.uuid4()}",
        auth_method="test",
    )


class TestSchema:
    def test_check_constraint_rejects_half_written_row(
        self, pg_engine: Engine, tenant_id: uuid.UUID, principal_id: uuid.UUID
    ) -> None:
        """Direct INSERT with 4 of 5 encryption columns set must fail."""
        with pg_engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO auth_profiles "
                    "(tenant_id, principal_id, id, provider, account_identifier, "
                    " backend, backend_key, "
                    " ciphertext, wrapped_dek, nonce, aad) "  # 4 of 5, missing kek_version
                    "VALUES "
                    "(:tid, :pid, 'broken', 'p', 'p', 'b', 'k', "
                    " :ct, :wd, :n, :a)"
                ),
                {
                    "tid": tenant_id,
                    "pid": principal_id,
                    "ct": b"ct",
                    "wd": b"wd",
                    "n": b"n",
                    "a": b"a",
                },
            )


@pytest.fixture()
def encryption_provider() -> InMemoryEncryptionProvider:
    return InMemoryEncryptionProvider()


@pytest.fixture()
def pg_store_crypto(
    pg_engine: Engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    encryption_provider: InMemoryEncryptionProvider,
) -> Generator[PostgresAuthProfileStore, None, None]:
    store = PostgresAuthProfileStore(
        PG_URL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=pg_engine,
        encryption_provider=encryption_provider,
    )
    yield store
    store.close()


class TestEncryptedUpsertAndGet:
    def test_roundtrip(self, pg_store_crypto: PostgresAuthProfileStore) -> None:
        profile = make_profile("google/alice")
        cred = ResolvedCredential(
            kind="bearer_token",
            access_token="ya29.fake",
            scopes=("https://www.googleapis.com/auth/userinfo.email",),
        )
        pg_store_crypto.upsert_with_credential(profile, cred)
        got = pg_store_crypto.get_with_credential("google/alice")
        assert got is not None
        p, c = got
        assert p.id == "google/alice"
        assert c is not None
        assert c.access_token == "ya29.fake"
        assert c.scopes == ("https://www.googleapis.com/auth/userinfo.email",)

    def test_get_returns_none_for_missing(self, pg_store_crypto: PostgresAuthProfileStore) -> None:
        assert pg_store_crypto.get_with_credential("does-not-exist") is None

    def test_pr1_row_returns_none_credential(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        """A row written via plain upsert reads back (profile, None)."""
        plain_store = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=principal_id,
            engine=pg_engine,
        )
        plain_store.upsert(make_profile("openai/bob"))
        got = pg_store_crypto.get_with_credential("openai/bob")
        assert got is not None
        p, c = got
        assert p.id == "openai/bob"
        assert c is None

    def test_ctor_without_provider_rejects_crypto_methods(
        self,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        store = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=principal_id,
            engine=pg_engine,
        )
        try:
            with pytest.raises(RuntimeError, match="encryption_provider"):
                store.upsert_with_credential(
                    make_profile("x"),
                    ResolvedCredential(kind="api_key", api_key="k"),
                )
            with pytest.raises(RuntimeError, match="encryption_provider"):
                store.get_with_credential("x")
        finally:
            store.close()


class TestSwapAttackRejected:
    def test_ciphertext_copied_cross_tenant_fails_decrypt(
        self,
        pg_engine: Engine,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Attacker with raw DB access copies A's ciphertext+wrapped_dek+nonce+aad+kek_version
        into B under a different tenant. Decrypt on B must fail.

        The InMemoryEncryptionProvider mixes tenant_id into AAD at the wrap
        level, so this raises WrappedDEKInvalid. Real providers fail at their
        native layer (KMS EncryptionContext, Vault derivation context). The
        stored ``aad`` column also wouldn't match B's ``tenant|principal|id``
        — AADMismatch would fire at the row-level check before unwrap.
        """
        t_a = ensure_tenant(pg_engine, f"atk-a-{uuid.uuid4()}")
        t_b = ensure_tenant(pg_engine, f"atk-b-{uuid.uuid4()}")
        p_a = ensure_principal(
            pg_engine, tenant_id=t_a, external_sub=f"sa-{uuid.uuid4()}", auth_method="t"
        )
        p_b = ensure_principal(
            pg_engine, tenant_id=t_b, external_sub=f"sb-{uuid.uuid4()}", auth_method="t"
        )
        store_a = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=t_a,
            principal_id=p_a,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )
        store_b = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=t_b,
            principal_id=p_b,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )
        try:
            store_a.upsert_with_credential(
                make_profile("shared-id"),
                ResolvedCredential(kind="api_key", api_key="A-SECRET"),
            )
            store_b.upsert(make_profile("shared-id"))
            with pg_engine.begin() as conn:
                conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t_a)})
                row_a = conn.execute(
                    text(
                        "SELECT ciphertext, wrapped_dek, nonce, aad, kek_version "
                        "FROM auth_profiles WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": t_a, "id": "shared-id"},
                ).fetchone()
            assert row_a is not None
            with pg_engine.begin() as conn:
                conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t_b)})
                conn.execute(
                    text(
                        "UPDATE auth_profiles SET "
                        "    ciphertext = :ct, wrapped_dek = :wd, "
                        "    nonce = :n, aad = :a, kek_version = :v "
                        "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                    ),
                    {
                        "ct": bytes(row_a.ciphertext),
                        "wd": bytes(row_a.wrapped_dek),
                        "n": bytes(row_a.nonce),
                        "a": bytes(row_a.aad),
                        "v": row_a.kek_version,
                        "tid": t_b,
                        "pid": p_b,
                        "id": "shared-id",
                    },
                )
            with pytest.raises((AADMismatch, WrappedDEKInvalid)):
                store_b.get_with_credential("shared-id")
        finally:
            store_a.close()
            store_b.close()


class TestMixedVersionReads:
    def test_reads_span_rotation(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("v1-row"),
            ResolvedCredential(kind="api_key", api_key="v1"),
        )
        encryption_provider.rotate()
        pg_store_crypto.upsert_with_credential(
            make_profile("v2-row"),
            ResolvedCredential(kind="api_key", api_key="v2"),
        )
        a = pg_store_crypto.get_with_credential("v1-row")
        b = pg_store_crypto.get_with_credential("v2-row")
        assert a is not None and a[1] is not None and a[1].api_key == "v1"
        assert b is not None and b[1] is not None and b[1].api_key == "v2"


class TestCacheAmortizes:
    def test_two_reads_one_unwrap(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("cached"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        start = encryption_provider.unwrap_count
        pg_store_crypto.get_with_credential("cached")
        pg_store_crypto.get_with_credential("cached")
        assert encryption_provider.unwrap_count - start == 1


class TestAADMismatch:
    def test_aad_column_tampered_raises(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("aad-tamper"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            conn.execute(
                text(
                    "UPDATE auth_profiles SET aad = :bad "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "bad": b"bogus-aad-bytes",
                    "tid": tenant_id,
                    "pid": principal_id,
                    "id": "aad-tamper",
                },
            )
        with pytest.raises(AADMismatch):
            pg_store_crypto.get_with_credential("aad-tamper")
