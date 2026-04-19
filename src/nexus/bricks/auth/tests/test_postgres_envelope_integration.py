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


from nexus.bricks.auth.postgres_profile_store import (  # noqa: E402
    rotate_kek_for_tenant,
)


class TestRotateKEKForTenant:
    def test_noop_when_all_rows_current(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("current-1"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
        )
        assert report.rows_rewrapped == 0
        assert report.rows_remaining == 0

    def test_rotates_stale_rows(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("a"),
            ResolvedCredential(kind="api_key", api_key="k-a"),
        )
        pg_store_crypto.upsert_with_credential(
            make_profile("b"),
            ResolvedCredential(kind="api_key", api_key="k-b"),
        )
        encryption_provider.rotate()
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
            batch_size=1,
        )
        assert report.rows_rewrapped == 2
        assert report.rows_remaining == 0
        assert report.target_version == 2
        a = pg_store_crypto.get_with_credential("a")
        b = pg_store_crypto.get_with_credential("b")
        assert a is not None and a[1] is not None and a[1].api_key == "k-a"
        assert b is not None and b[1] is not None and b[1].api_key == "k-b"

    def test_respects_max_rows(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"m-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
            batch_size=2,
            max_rows=2,
        )
        assert report.rows_rewrapped == 2
        assert report.rows_remaining == 1


class TestRotateKEKFailures:
    def test_per_row_failure_continues_batch(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """An unwrap failure on one row leaves that row on the old version; the
        batch continues to completion for other rows."""
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"fail-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()

        middle_wrapped = None
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            row = conn.execute(
                text(
                    "SELECT wrapped_dek FROM auth_profiles WHERE tenant_id = :tid AND id = 'fail-1'"
                ),
                {"tid": tenant_id},
            ).fetchone()
            assert row is not None
            middle_wrapped = bytes(row.wrapped_dek)

        real = encryption_provider

        class _FlakyProvider:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                if wrapped == middle_wrapped:
                    raise WrappedDEKInvalid.from_row(
                        tenant_id=tenant_id,
                        profile_id="fail-1",
                        kek_version=kek_version,
                        cause="simulated flake",
                    )
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=_FlakyProvider(),
        )
        assert report.rows_rewrapped == 2
        assert report.rows_failed == 1
        assert report.rows_remaining == 1

    def test_wrap_at_target_failure_aborts_batch(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression: a wrap failure at the target version must abort the
        batch rather than continue per-row. Otherwise a misconfigured target
        KEK leaves the tenant with rows split across versions.
        """
        from nexus.bricks.auth.envelope import EnvelopeConfigurationError

        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"wrap-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()

        real = encryption_provider

        class _WrapFailsProvider:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):  # noqa: ARG002
                raise EnvelopeConfigurationError("simulated bad target KEK")

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        with pytest.raises(EnvelopeConfigurationError):
            rotate_kek_for_tenant(
                pg_engine,
                tenant_id=tenant_id,
                encryption_provider=_WrapFailsProvider(),
            )

        # No rows should be committed: every row must still be at v1.
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            versions = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT DISTINCT kek_version FROM auth_profiles "
                        "WHERE tenant_id = :tid AND ciphertext IS NOT NULL"
                    ),
                    {"tid": tenant_id},
                ).fetchall()
            ]
        assert versions == [1], "wrap-fatal must not leave partial rewraps committed"

    def test_concurrent_writer_does_not_block_on_slow_provider(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression: rotation must not hold row locks across provider calls.

        Simulates a slow KMS/Vault call by sleeping inside wrap_dek. A
        concurrent upsert_with_credential on the same row must be able to
        commit without waiting for the provider RTT to finish.
        """
        import threading
        import time

        pg_store_crypto.upsert_with_credential(
            make_profile("slow-provider"),
            ResolvedCredential(kind="api_key", api_key="v1"),
        )
        encryption_provider.rotate()

        real = encryption_provider
        rotation_started = threading.Event()

        class _SlowProvider:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                rotation_started.set()
                time.sleep(2.0)  # simulate a slow KMS RTT
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        def _rotate() -> None:
            rotate_kek_for_tenant(
                pg_engine,
                tenant_id=tenant_id,
                encryption_provider=_SlowProvider(),
            )

        t = threading.Thread(target=_rotate)
        t.start()
        try:
            assert rotation_started.wait(timeout=5.0), "rotation failed to start"
            # Rotation is now inside the slow wrap_dek. A concurrent write
            # must not block on row locks; it should commit in well under the
            # provider delay.
            write_start = time.monotonic()
            pg_store_crypto.upsert_with_credential(
                make_profile("slow-provider"),
                ResolvedCredential(kind="api_key", api_key="v2"),
            )
            write_elapsed = time.monotonic() - write_start
            assert write_elapsed < 1.0, (
                f"concurrent write took {write_elapsed:.2f}s while rotation held "
                "locks across the provider call — lock scope too wide"
            )
        finally:
            t.join(timeout=10)

    def test_max_rows_does_not_count_failures(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression: --max-rows caps successful rewraps, not total rows seen.

        Previously ``max_rows=1`` would exit after one failure (rewrapped=0,
        failed=1), starving healthy rows. Now failures are tracked separately
        and the budget applies only to successful rewraps.
        """
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"budget-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()

        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            row = conn.execute(
                text(
                    "SELECT wrapped_dek FROM auth_profiles "
                    "WHERE tenant_id = :tid AND id = 'budget-0'"
                ),
                {"tid": tenant_id},
            ).fetchone()
            assert row is not None
            bad_wrapped = bytes(row.wrapped_dek)

        real = encryption_provider

        class _FailFirst:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                if wrapped == bad_wrapped:
                    raise WrappedDEKInvalid.from_row(
                        tenant_id=tenant_id,
                        profile_id="budget-0",
                        kek_version=kek_version,
                        cause="simulated",
                    )
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=_FailFirst(),
            batch_size=1,
            max_rows=1,
        )
        assert report.rows_rewrapped == 1, "one healthy row must rewrap within budget"
        assert report.rows_failed == 1, "failure observed but does not consume budget"

    def test_version_skew_raises_by_default(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression: helper must raise VersionSkewError when rows exist at a
        higher version than the provider's current, not silently report
        success.
        """
        from nexus.bricks.auth.postgres_profile_store import VersionSkewError

        pg_store_crypto.upsert_with_credential(
            make_profile("skew-helper"),
            ResolvedCredential(kind="api_key", api_key="s"),
        )
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            conn.execute(
                text(
                    "UPDATE auth_profiles SET kek_version = 99 "
                    "WHERE tenant_id = :tid AND id = 'skew-helper'"
                ),
                {"tid": tenant_id},
            )

        with pytest.raises(VersionSkewError) as exc_info:
            rotate_kek_for_tenant(
                pg_engine,
                tenant_id=tenant_id,
                encryption_provider=encryption_provider,
            )
        assert exc_info.value.rows_ahead == 1

        # allow_skew=True suppresses the check
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
            allow_skew=True,
        )
        assert report.rows_rewrapped == 0  # nothing to rewrap — only ahead row exists
        assert report.rows_remaining == 0

    def test_replace_owned_subset_rejects_encrypted_row(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,  # noqa: ARG002
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        """Regression: replace_owned_subset shares the UPSERT path with plain
        upsert, so it must enforce the same encrypted-row guard. Otherwise a
        sync path (used by external_sync.registry) could rewrite backend_key
        while leaving old ciphertext stored.
        """
        pg_store_crypto.upsert_with_credential(
            make_profile("sync-row"),
            ResolvedCredential(kind="api_key", api_key="s"),
        )
        new_profile = make_profile("sync-row", backend_key="secret://different")

        plain_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tenant_id, principal_id=principal_id
        )
        try:
            with pytest.raises(ValueError, match="encrypted"):
                plain_store.replace_owned_subset(upserts=[new_profile], deletes=[])
        finally:
            plain_store.close()

    def test_plain_upsert_rejects_encrypted_row(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,  # noqa: ARG002
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        """Regression: plain upsert() cannot mutate routing metadata on a row
        that already carries encrypted credentials, because the old ciphertext
        would no longer match the updated backend_key.
        """
        pg_store_crypto.upsert_with_credential(
            make_profile("enc-row"),
            ResolvedCredential(kind="api_key", api_key="s"),
        )
        new_profile = make_profile("enc-row", backend_key="secret://different")

        # Use a non-crypto store pointing at the same row — simulates a
        # pre-envelope caller still holding the plain upsert path.
        plain_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tenant_id, principal_id=principal_id
        )
        try:
            with pytest.raises(ValueError, match="encrypted credentials"):
                plain_store.upsert(new_profile)
        finally:
            plain_store.close()

    def test_rejects_non_positive_batch_size(
        self,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Defensive guard: batch_size <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            rotate_kek_for_tenant(
                pg_engine,
                tenant_id=tenant_id,
                encryption_provider=encryption_provider,
                batch_size=0,
            )

    def test_rejects_non_positive_max_rows(
        self,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Defensive guard: max_rows <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="max_rows must be >= 1"):
            rotate_kek_for_tenant(
                pg_engine,
                tenant_id=tenant_id,
                encryption_provider=encryption_provider,
                max_rows=0,
            )

    def test_aad_tampered_row_fails_rotation(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression: a provider like Vault Transit ignores AAD bytes during
        unwrap/wrap, so an attacker who tampered the ``aad`` column could
        silently rotate to the new version and only fail later at the AESGCM
        read path. Rotation must independently re-validate AAD against
        ``(tenant_id, principal_id, id)``.
        """
        pg_store_crypto.upsert_with_credential(
            make_profile("aad-rotate"),
            ResolvedCredential(kind="api_key", api_key="secret"),
        )
        encryption_provider.rotate()

        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            conn.execute(
                text(
                    "UPDATE auth_profiles SET aad = :aad "
                    "WHERE tenant_id = :tid AND id = 'aad-rotate'"
                ),
                {"tid": tenant_id, "aad": b"tampered-aad"},
            )

        real = encryption_provider

        class _AadIgnoringProvider:
            """Mimics Vault Transit: ignores aad bytes entirely during un/wrap."""

            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):  # noqa: ARG002
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=b"")

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):  # noqa: ARG002
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=b"", kek_version=kek_version
                )

        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=_AadIgnoringProvider(),
        )

        assert report.rows_rewrapped == 0, "AAD-tampered row must not rotate"
        assert report.rows_failed == 1, "AAD-tampered row must be counted as failed"

    def test_skiplist_uses_composite_principal_id_and_id(
        self,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Regression for Codex finding: skip-list must key on (principal_id, id).

        Two principals in the same tenant each own a profile with the same
        ``id="shared"``. If the failing row is skipped by ``id`` alone, the
        *other* principal's row with the same id is also hidden from the next
        SELECT and never rotates.
        """
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        p_a = ensure_principal(
            pg_engine,
            tenant_id=tenant_id,
            kind="human",
            external_sub=f"skip-a-{uuid.uuid4()}",
            auth_method="test",
        )
        p_b = ensure_principal(
            pg_engine,
            tenant_id=tenant_id,
            kind="human",
            external_sub=f"skip-b-{uuid.uuid4()}",
            auth_method="test",
        )

        store_a = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=p_a,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )
        store_b = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=p_b,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )

        store_a.upsert_with_credential(
            make_profile("shared-id"),
            ResolvedCredential(kind="api_key", api_key="secret-a"),
        )
        store_b.upsert_with_credential(
            make_profile("shared-id"),
            ResolvedCredential(kind="api_key", api_key="secret-b"),
        )

        encryption_provider.rotate()

        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})
            row = conn.execute(
                text(
                    "SELECT wrapped_dek FROM auth_profiles WHERE tenant_id = :tid "
                    "AND principal_id = :pid AND id = 'shared-id'"
                ),
                {"tid": tenant_id, "pid": p_a},
            ).fetchone()
            assert row is not None
            bad_wrapped = bytes(row.wrapped_dek)

        real = encryption_provider

        class _FlakyForPrincipalA:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                if wrapped == bad_wrapped:
                    raise WrappedDEKInvalid.from_row(
                        tenant_id=tenant_id,
                        profile_id="shared-id",
                        kek_version=kek_version,
                        cause="simulated principal-a failure",
                    )
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=_FlakyForPrincipalA(),
            batch_size=1,
        )

        assert report.rows_failed == 1
        assert report.rows_rewrapped == 1, (
            "principal_b's row with the same id must rotate; skip-list keyed "
            "on id alone would hide it after principal_a's failure"
        )
