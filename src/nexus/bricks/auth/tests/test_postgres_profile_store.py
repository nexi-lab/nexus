"""Tests for PostgresAuthProfileStore (epic #3788, PR 1).

Mirrors test_profile_store.py coverage against Postgres. Adds:
  - tenant isolation (two tenants cannot see each other's rows)
  - principal isolation (two principals in one tenant cannot see each other's rows)
  - schema idempotency (ensure_schema can run twice)

Requires a running Postgres at ``TEST_POSTGRES_URL`` (default
``postgresql+psycopg2://postgres:nexus@localhost:5432/nexus``). Tests are
skipped when unavailable — bring one up with
``docker compose -f dockerfiles/compose.yaml up postgres -d``.

Tables are created in a dedicated schema per module so the suite does not
collide with other Postgres-marked tests touching unrelated tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.profile import (
    RAW_ERROR_MAX_LEN,
    AuthProfileFailureReason,
)
from nexus.bricks.auth.tests.conftest import make_profile

# ---------------------------------------------------------------------------
# Postgres availability gate
# ---------------------------------------------------------------------------

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    try:
        engine = create_engine(PG_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    # Pin the whole module to one xdist worker so concurrent workers don't
    # race on ``CREATE TABLE IF NOT EXISTS`` (pg_type UniqueViolation).
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason=(
            "PostgreSQL not reachable at TEST_POSTGRES_URL. "
            "Start with: docker compose -f dockerfiles/compose.yaml up postgres -d"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    """Shared engine with clean schema per module run."""
    engine = create_engine(PG_URL, future=True)
    drop_schema(engine)
    ensure_schema(engine)
    yield engine
    drop_schema(engine)
    engine.dispose()


@pytest.fixture()
def tenant_id(pg_engine: Engine) -> uuid.UUID:
    """Unique tenant per test (avoids cross-test pollution)."""
    return ensure_tenant(pg_engine, f"test-tenant-{uuid.uuid4()}")


@pytest.fixture()
def principal_id(pg_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    return ensure_principal(
        pg_engine,
        tenant_id=tenant_id,
        kind="human",
        external_sub=f"sub-{uuid.uuid4()}",
        auth_method="test",
    )


@pytest.fixture()
def pg_store(
    pg_engine: Engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> Generator[PostgresAuthProfileStore, None, None]:
    store = PostgresAuthProfileStore(
        PG_URL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=pg_engine,
    )
    yield store
    store.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_upsert_and_get(self, pg_store: PostgresAuthProfileStore) -> None:
        p = make_profile("openai/alice", provider="openai")
        pg_store.upsert(p)
        result = pg_store.get("openai/alice")
        assert result is not None
        assert result.id == "openai/alice"
        assert result.provider == "openai"
        assert result.backend == "nexus-token-manager"

    def test_get_nonexistent(self, pg_store: PostgresAuthProfileStore) -> None:
        assert pg_store.get("nope") is None

    def test_upsert_updates_existing(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("p1", backend_key="old-key"))
        pg_store.upsert(make_profile("p1", backend_key="new-key"))
        result = pg_store.get("p1")
        assert result is not None
        assert result.backend_key == "new-key"

    def test_delete(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("p1"))
        pg_store.delete("p1")
        assert pg_store.get("p1") is None

    def test_delete_nonexistent_is_noop(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.delete("nope")

    def test_list_all(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("a", provider="openai"))
        pg_store.upsert(make_profile("b", provider="anthropic"))
        pg_store.upsert(make_profile("c", provider="openai"))
        ids = sorted(p.id for p in pg_store.list())
        assert ids == ["a", "b", "c"]

    def test_list_filtered_by_provider(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("a", provider="openai"))
        pg_store.upsert(make_profile("b", provider="anthropic"))
        pg_store.upsert(make_profile("c", provider="openai"))
        openai_ids = sorted(p.id for p in pg_store.list(provider="openai"))
        assert openai_ids == ["a", "c"]

    def test_replace_owned_subset_atomic(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("old1", provider="test"))
        pg_store.upsert(make_profile("old2", provider="test"))
        assert {p.id for p in pg_store.list()} == {"old1", "old2"}

        pg_store.replace_owned_subset(
            upserts=[make_profile("new3", provider="test")],
            deletes=["old2"],
        )

        ids = {p.id for p in pg_store.list()}
        assert ids == {"old1", "new3"}
        assert pg_store.get("old2") is None
        assert pg_store.get("new3") is not None

    def test_replace_owned_subset_empty_is_noop(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("keep", provider="test"))
        pg_store.replace_owned_subset(upserts=[], deletes=[])
        assert pg_store.get("keep") is not None


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------


class TestUsageStats:
    def test_mark_success_increments_count(self, pg_store: PostgresAuthProfileStore) -> None:
        pg_store.upsert(make_profile("p1"))
        pg_store.mark_success("p1")
        pg_store.mark_success("p1")
        result = pg_store.get("p1")
        assert result is not None
        assert result.usage_stats.success_count == 2
        assert result.usage_stats.last_used_at is not None

    def test_mark_success_clears_expired_cooldown(self, pg_store: PostgresAuthProfileStore) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        pg_store.upsert(
            make_profile(
                "p1",
                cooldown_until=past,
                cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
            )
        )
        pg_store.mark_success("p1")
        result = pg_store.get("p1")
        assert result is not None
        assert result.usage_stats.cooldown_until is None
        assert result.usage_stats.cooldown_reason is None

    def test_mark_success_preserves_active_cooldown(
        self, pg_store: PostgresAuthProfileStore
    ) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        pg_store.upsert(
            make_profile(
                "p1",
                cooldown_until=future,
                cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
            )
        )
        pg_store.mark_success("p1")
        result = pg_store.get("p1")
        assert result is not None
        assert result.usage_stats.cooldown_until is not None
        assert result.usage_stats.cooldown_reason == AuthProfileFailureReason.RATE_LIMIT

    def test_mark_failure_increments_count_and_records_reason(
        self, pg_store: PostgresAuthProfileStore
    ) -> None:
        pg_store.upsert(make_profile("p1"))
        pg_store.mark_failure("p1", AuthProfileFailureReason.AUTH)
        result = pg_store.get("p1")
        assert result is not None
        assert result.usage_stats.failure_count == 1
        assert result.usage_stats.cooldown_reason == AuthProfileFailureReason.AUTH

    def test_mark_failure_truncates_raw_error(self, pg_store: PostgresAuthProfileStore) -> None:
        long_err = "x" * (RAW_ERROR_MAX_LEN + 200)
        pg_store.upsert(make_profile("p1"))
        pg_store.mark_failure("p1", AuthProfileFailureReason.UNKNOWN, raw_error=long_err)
        result = pg_store.get("p1")
        assert result is not None
        assert result.usage_stats.raw_error is not None
        assert len(result.usage_stats.raw_error) == RAW_ERROR_MAX_LEN


# ---------------------------------------------------------------------------
# Isolation (the whole point of this store)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_two_tenants_cannot_see_each_others_rows(self, pg_engine: Engine) -> None:
        t1 = ensure_tenant(pg_engine, f"iso-a-{uuid.uuid4()}")
        t2 = ensure_tenant(pg_engine, f"iso-b-{uuid.uuid4()}")
        p1 = ensure_principal(
            pg_engine,
            tenant_id=t1,
            external_sub=f"p1-{uuid.uuid4()}",
            auth_method="test",
        )
        p2 = ensure_principal(
            pg_engine,
            tenant_id=t2,
            external_sub=f"p2-{uuid.uuid4()}",
            auth_method="test",
        )

        store1 = PostgresAuthProfileStore(PG_URL, tenant_id=t1, principal_id=p1, engine=pg_engine)
        store2 = PostgresAuthProfileStore(PG_URL, tenant_id=t2, principal_id=p2, engine=pg_engine)
        try:
            store1.upsert(make_profile("shared-id", provider="openai"))
            store2.upsert(make_profile("shared-id", provider="anthropic", backend_key="t2-key"))

            r1 = store1.get("shared-id")
            r2 = store2.get("shared-id")
            assert r1 is not None and r1.provider == "openai"
            assert r2 is not None and r2.provider == "anthropic"

            # Each tenant sees exactly its own row
            assert [p.id for p in store1.list()] == ["shared-id"]
            assert [p.id for p in store2.list()] == ["shared-id"]
        finally:
            store1.close()
            store2.close()


class TestPrincipalIsolation:
    def test_two_principals_in_one_tenant_do_not_leak(self, pg_engine: Engine) -> None:
        tid = ensure_tenant(pg_engine, f"shared-tenant-{uuid.uuid4()}")
        alice = ensure_principal(
            pg_engine,
            tenant_id=tid,
            external_sub=f"alice-{uuid.uuid4()}",
            auth_method="test",
        )
        bob = ensure_principal(
            pg_engine,
            tenant_id=tid,
            external_sub=f"bob-{uuid.uuid4()}",
            auth_method="test",
        )

        alice_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tid, principal_id=alice, engine=pg_engine
        )
        bob_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tid, principal_id=bob, engine=pg_engine
        )
        try:
            alice_store.upsert(make_profile("gmail/alice", provider="google"))
            bob_store.upsert(make_profile("gmail/bob", provider="google"))

            alice_ids = [p.id for p in alice_store.list()]
            bob_ids = [p.id for p in bob_store.list()]
            assert alice_ids == ["gmail/alice"]
            assert bob_ids == ["gmail/bob"]

            # Bob cannot get Alice's profile by ID even though tenant is shared
            assert bob_store.get("gmail/alice") is None
            assert alice_store.get("gmail/bob") is None
        finally:
            alice_store.close()
            bob_store.close()


# ---------------------------------------------------------------------------
# Schema idempotency
# ---------------------------------------------------------------------------


class TestAliasTenantScoping:
    """Aliases must be tenant-scoped. The same ``(auth_method, external_sub)``
    in two different tenants must resolve to two different principals —
    otherwise a shared OIDC sub would collapse identities across the very
    tenant boundary this epic exists to enforce."""

    def test_same_alias_in_two_tenants_yields_distinct_principals(self, pg_engine: Engine) -> None:
        t1 = ensure_tenant(pg_engine, f"alias-t1-{uuid.uuid4()}")
        t2 = ensure_tenant(pg_engine, f"alias-t2-{uuid.uuid4()}")
        shared_sub = f"alice-{uuid.uuid4()}"
        p1 = ensure_principal(
            pg_engine,
            tenant_id=t1,
            external_sub=shared_sub,
            auth_method="oidc",
        )
        p2 = ensure_principal(
            pg_engine,
            tenant_id=t2,
            external_sub=shared_sub,
            auth_method="oidc",
        )
        assert p1 != p2

    def test_same_alias_in_same_tenant_dedups(self, pg_engine: Engine) -> None:
        tid = ensure_tenant(pg_engine, f"alias-dedup-{uuid.uuid4()}")
        sub = f"alice-{uuid.uuid4()}"
        a = ensure_principal(pg_engine, tenant_id=tid, external_sub=sub, auth_method="oidc")
        b = ensure_principal(pg_engine, tenant_id=tid, external_sub=sub, auth_method="oidc")
        assert a == b


class TestAuthProfilesTenantPrincipalFK:
    """``auth_profiles`` must reject a row whose ``tenant_id`` and
    ``principal_id`` refer to different principals — same invariant as
    ``principal_aliases``. The composite FK
    ``(principal_id, tenant_id) -> principals(id, tenant_id)`` is the
    database-level defense."""

    def test_mismatched_tenant_principal_is_rejected(self, pg_engine: Engine) -> None:
        t1 = ensure_tenant(pg_engine, f"fk-a-{uuid.uuid4()}")
        t2 = ensure_tenant(pg_engine, f"fk-b-{uuid.uuid4()}")
        p_in_t1 = ensure_principal(
            pg_engine,
            tenant_id=t1,
            external_sub=f"p1-{uuid.uuid4()}",
            auth_method="test",
        )
        # Tenant 2 must exist so the malformed insert is rejected by the
        # FK and not by a missing tenant row.
        assert t2 != t1
        with pytest.raises(IntegrityError), pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO auth_profiles ("
                    "    tenant_id, principal_id, id, provider, "
                    "    account_identifier, backend, backend_key) "
                    "VALUES (:tid, :pid, 'bad/row', 'openai', "
                    "    'x', 'nexus-token-manager', 'k')"
                ),
                {"tid": t2, "pid": p_in_t1},
            )


class TestPrincipalUpsertOwnership:
    """Upsert must not silently reassign a row from one principal to another
    within the same tenant. The composite PK ``(tenant_id, principal_id, id)``
    makes same-id + different-principal produce two distinct rows instead."""

    def test_same_id_across_principals_creates_two_rows(self, pg_engine: Engine) -> None:
        tid = ensure_tenant(pg_engine, f"owner-tenant-{uuid.uuid4()}")
        alice = ensure_principal(
            pg_engine,
            tenant_id=tid,
            external_sub=f"alice-{uuid.uuid4()}",
            auth_method="test",
        )
        bob = ensure_principal(
            pg_engine,
            tenant_id=tid,
            external_sub=f"bob-{uuid.uuid4()}",
            auth_method="test",
        )
        alice_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tid, principal_id=alice, engine=pg_engine
        )
        bob_store = PostgresAuthProfileStore(
            PG_URL, tenant_id=tid, principal_id=bob, engine=pg_engine
        )
        try:
            alice_store.upsert(make_profile("shared-id", backend_key="alice-key"))
            bob_store.upsert(make_profile("shared-id", backend_key="bob-key"))

            # Each principal sees only their own row — bob's upsert did NOT
            # take ownership of alice's row.
            alice_row = alice_store.get("shared-id")
            bob_row = bob_store.get("shared-id")
            assert alice_row is not None and alice_row.backend_key == "alice-key"
            assert bob_row is not None and bob_row.backend_key == "bob-key"
        finally:
            alice_store.close()
            bob_store.close()


class TestSchemaUpgrade:
    """``ensure_schema`` must also upgrade a pre-composite-PK shape in place.

    Simulates an install that bootstrapped with the earlier DDL (no
    ``tenant_id`` on ``principal_aliases``; PK ``(tenant_id, id)`` on
    ``auth_profiles``) and asserts that running the current ``ensure_schema``
    brings it to the current shape with data preserved and new invariants
    enforceable (same id under two principals coexists).
    """

    def test_upgrades_legacy_shape_in_place(self, pg_engine: Engine) -> None:
        legacy_suffix = uuid.uuid4().hex[:8]
        conn_prefix = f"legacy_{legacy_suffix}_"

        # Create the schema using pg_engine (shared); everything else runs
        # on a dedicated engine whose search_path is pinned via
        # connect_args. ``SET search_path`` on a pooled connection leaks
        # across tests because it is session-scoped, so we keep that out
        # of the shared pool.
        with pg_engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {conn_prefix}sch"))

        legacy_engine = create_engine(
            PG_URL,
            future=True,
            connect_args={"options": f"-csearch_path={conn_prefix}sch"},
        )

        # Seed legacy-shape tables + one row each, via legacy_engine.
        with legacy_engine.begin() as conn:
            # Minimal legacy DDL: no tenant_id on aliases; composite
            # auth_profiles PK only (tenant_id, id).
            conn.execute(
                text(
                    "CREATE TABLE tenants ("
                    "id UUID PRIMARY KEY, "
                    "name TEXT NOT NULL UNIQUE, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE principals ("
                    "id UUID PRIMARY KEY, "
                    "tenant_id UUID NOT NULL REFERENCES tenants(id), "
                    "kind TEXT NOT NULL, "
                    "parent_principal_id UUID REFERENCES principals(id), "
                    "delegated_scope JSONB, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE principal_aliases ("
                    "auth_method TEXT NOT NULL, "
                    "external_sub TEXT NOT NULL, "
                    "principal_id UUID NOT NULL REFERENCES principals(id), "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                    "PRIMARY KEY (auth_method, external_sub))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE auth_profiles ("
                    "tenant_id UUID NOT NULL REFERENCES tenants(id), "
                    "id TEXT NOT NULL, "
                    "principal_id UUID NOT NULL REFERENCES principals(id), "
                    "provider TEXT NOT NULL, "
                    "account_identifier TEXT NOT NULL, "
                    "backend TEXT NOT NULL, "
                    "backend_key TEXT NOT NULL, "
                    "last_synced_at TIMESTAMPTZ, "
                    "sync_ttl_seconds INTEGER NOT NULL DEFAULT 300, "
                    "last_used_at TIMESTAMPTZ, "
                    "success_count INTEGER NOT NULL DEFAULT 0, "
                    "failure_count INTEGER NOT NULL DEFAULT 0, "
                    "cooldown_until TIMESTAMPTZ, "
                    "cooldown_reason TEXT, "
                    "disabled_until TIMESTAMPTZ, "
                    "raw_error TEXT, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
                    "PRIMARY KEY (tenant_id, id))"
                )
            )
            # Seed one tenant + principal + alias + profile under the legacy
            # shape so we can confirm data is preserved.
            tid = uuid.uuid4()
            pid = uuid.uuid4()
            conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (:tid, 'legacy')"),
                {"tid": tid},
            )
            conn.execute(
                text("INSERT INTO principals (id, tenant_id, kind) VALUES (:pid, :tid, 'human')"),
                {"pid": pid, "tid": tid},
            )
            conn.execute(
                text(
                    "INSERT INTO principal_aliases "
                    "    (auth_method, external_sub, principal_id) "
                    "VALUES ('legacy', 'legacy-sub', :pid)"
                ),
                {"pid": pid},
            )
            conn.execute(
                text(
                    "INSERT INTO auth_profiles "
                    "    (tenant_id, id, principal_id, provider, "
                    "     account_identifier, backend, backend_key) "
                    "VALUES (:tid, 'legacy/row', :pid, 'legacy', "
                    "        'alice', 'nexus-token-manager', 'key')"
                ),
                {"tid": tid, "pid": pid},
            )

        try:
            ensure_schema(legacy_engine)

            # Verify the new shape: principal_aliases has tenant_id, PK
            # migrated; auth_profiles PK now includes principal_id.
            with legacy_engine.begin() as conn:
                alias_has_tid = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = :sch "
                        "  AND table_name = 'principal_aliases' "
                        "  AND column_name = 'tenant_id'"
                    ),
                    {"sch": f"{conn_prefix}sch"},
                ).fetchone()
                assert alias_has_tid is not None

                pk_cols = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT kcu.column_name "
                            "FROM information_schema.table_constraints tc "
                            "JOIN information_schema.key_column_usage kcu "
                            "  ON tc.constraint_name = kcu.constraint_name "
                            " AND tc.table_schema = kcu.table_schema "
                            "WHERE tc.table_schema = :sch "
                            "  AND tc.table_name = 'auth_profiles' "
                            "  AND tc.constraint_type = 'PRIMARY KEY'"
                        ),
                        {"sch": f"{conn_prefix}sch"},
                    ).fetchall()
                }
                assert pk_cols == {"tenant_id", "principal_id", "id"}

                # Legacy row preserved — value matches the seed.
                seeded = conn.execute(
                    text("SELECT backend_key FROM auth_profiles WHERE id = 'legacy/row'")
                ).fetchone()
                assert seeded is not None and seeded[0] == "key"

                # Composite (principal_id, tenant_id) FK must be present on
                # upgraded installs — otherwise a malformed alias could
                # point at a principal in a different tenant.
                has_composite_fk = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.referential_constraints rc "
                        "JOIN information_schema.key_column_usage kcu "
                        "  ON rc.constraint_name = kcu.constraint_name "
                        " AND rc.constraint_schema = kcu.table_schema "
                        "WHERE kcu.table_schema = :sch "
                        "  AND kcu.table_name = 'principal_aliases' "
                        "  AND kcu.column_name IN ('principal_id', 'tenant_id') "
                        "GROUP BY rc.constraint_name "
                        "HAVING COUNT(DISTINCT kcu.column_name) = 2"
                    ),
                    {"sch": f"{conn_prefix}sch"},
                ).fetchone()
                assert has_composite_fk is not None, (
                    "composite (principal_id, tenant_id) FK missing after upgrade"
                )

            # Negative test: inserting an alias whose tenant_id does not
            # match the principal's tenant_id must be rejected by the FK.
            other_tid = uuid.uuid4()
            with legacy_engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO tenants (id, name) VALUES (:tid, 'other')"),
                    {"tid": other_tid},
                )
            with pytest.raises(IntegrityError), legacy_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO principal_aliases "
                        "    (tenant_id, auth_method, external_sub, principal_id) "
                        "SELECT :other_tid, 'malformed', 'x', id "
                        "FROM principals LIMIT 1"
                    ),
                    {"other_tid": other_tid},
                )
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text(f"DROP SCHEMA {conn_prefix}sch CASCADE"))
            legacy_engine.dispose()


class TestSchema:
    def test_ensure_schema_is_idempotent(self, pg_engine: Engine) -> None:
        # Already run by the module fixture — running again must not raise.
        ensure_schema(pg_engine)
        ensure_schema(pg_engine)

    def test_ensure_tenant_returns_same_id_for_same_name(self, pg_engine: Engine) -> None:
        name = f"dedup-{uuid.uuid4()}"
        a = ensure_tenant(pg_engine, name)
        b = ensure_tenant(pg_engine, name)
        assert a == b

    def test_ensure_principal_returns_same_id_for_same_alias(self, pg_engine: Engine) -> None:
        tid = ensure_tenant(pg_engine, f"dedup-p-{uuid.uuid4()}")
        sub = f"sub-{uuid.uuid4()}"
        a = ensure_principal(pg_engine, tenant_id=tid, external_sub=sub, auth_method="oidc")
        b = ensure_principal(pg_engine, tenant_id=tid, external_sub=sub, auth_method="oidc")
        assert a == b
