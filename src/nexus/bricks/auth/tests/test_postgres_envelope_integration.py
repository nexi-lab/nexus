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

from nexus.bricks.auth.postgres_profile_store import (
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)

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
