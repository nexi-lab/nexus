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
