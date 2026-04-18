"""Tests for SqliteAuthProfileStore → PostgresAuthProfileStore migration.

Covers:
  - Dry-run plan (no target writes)
  - Apply copies rows with stats preserved
  - Skip-when-exists (default); --force overwrites
  - Plan/apply drift: row deleted between plan and apply surfaces as error

Shares the Postgres availability gate and fixtures with
test_postgres_profile_store (same xdist_group so both serialize on a single
worker — avoids ``CREATE TABLE IF NOT EXISTS`` races on pg_type).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.postgres_migrate import (
    build_migration_plan,
    execute_migration,
)
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
from nexus.bricks.auth.tests.conftest import make_profile

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
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason="PostgreSQL not reachable at TEST_POSTGRES_URL",
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
def sqlite_source() -> Generator[SqliteAuthProfileStore, None, None]:
    store = SqliteAuthProfileStore(":memory:")
    yield store
    store.close()


@pytest.fixture()
def pg_target(pg_engine: Engine) -> Generator[PostgresAuthProfileStore, None, None]:
    tenant_id = ensure_tenant(pg_engine, f"mig-tenant-{uuid.uuid4()}")
    principal_id = ensure_principal(
        pg_engine,
        tenant_id=tenant_id,
        external_sub=f"mig-sub-{uuid.uuid4()}",
        auth_method="test",
    )
    store = PostgresAuthProfileStore(
        PG_URL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=pg_engine,
    )
    yield store
    store.close()


class TestPlan:
    def test_empty_source_yields_empty_plan(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        plan = build_migration_plan(sqlite_source, pg_target)
        assert plan == []

    def test_copy_action_when_target_empty(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a"))
        sqlite_source.upsert(make_profile("anthropic/b", provider="anthropic"))
        plan = build_migration_plan(sqlite_source, pg_target)
        actions = {e.profile_id: e.action for e in plan}
        assert actions == {"openai/a": "copy", "anthropic/b": "copy"}

    def test_skip_exists_by_default(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a"))
        pg_target.upsert(make_profile("openai/a", backend_key="already-there"))
        plan = build_migration_plan(sqlite_source, pg_target)
        assert len(plan) == 1
        assert plan[0].action == "skip_exists"

    def test_force_schedules_overwrite(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a"))
        pg_target.upsert(make_profile("openai/a", backend_key="already-there"))
        plan = build_migration_plan(sqlite_source, pg_target, force=True)
        assert len(plan) == 1
        assert plan[0].action == "overwrite"


class TestExecute:
    def test_dry_run_does_not_write(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a"))
        plan = build_migration_plan(sqlite_source, pg_target)
        result = execute_migration(plan, sqlite_source, pg_target, apply=False)
        assert result.copied == 1
        assert result.dry_run is True
        assert pg_target.get("openai/a") is None

    def test_apply_copies_row(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a", backend_key="src-key"))
        plan = build_migration_plan(sqlite_source, pg_target)
        result = execute_migration(plan, sqlite_source, pg_target, apply=True)
        assert result.copied == 1
        assert result.errors == 0
        copied = pg_target.get("openai/a")
        assert copied is not None
        assert copied.backend_key == "src-key"

    def test_apply_preserves_usage_stats(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a", success_count=7, failure_count=3))
        plan = build_migration_plan(sqlite_source, pg_target)
        execute_migration(plan, sqlite_source, pg_target, apply=True)
        copied = pg_target.get("openai/a")
        assert copied is not None
        assert copied.usage_stats.success_count == 7
        assert copied.usage_stats.failure_count == 3

    def test_force_overwrites_existing_target_row(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a", backend_key="src-key"))
        pg_target.upsert(make_profile("openai/a", backend_key="stale-key"))
        plan = build_migration_plan(sqlite_source, pg_target, force=True)
        execute_migration(plan, sqlite_source, pg_target, apply=True)
        copied = pg_target.get("openai/a")
        assert copied is not None
        assert copied.backend_key == "src-key"

    def test_drift_between_plan_and_apply_is_surfaced(
        self,
        sqlite_source: SqliteAuthProfileStore,
        pg_target: PostgresAuthProfileStore,
    ) -> None:
        sqlite_source.upsert(make_profile("openai/a"))
        plan = build_migration_plan(sqlite_source, pg_target)
        # Row disappears before apply.
        sqlite_source.delete("openai/a")
        result = execute_migration(plan, sqlite_source, pg_target, apply=True)
        assert result.errors == 1
        assert result.copied == 0
        assert result.entries[0].action == "error"
