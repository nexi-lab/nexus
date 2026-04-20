"""Shared fixtures for auth integration tests (#3804)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.postgres_profile_store import ensure_schema

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://tafeng@127.0.0.1:5432/nexus_e2e_test",
)


@pytest.fixture(scope="module")
def pg_engine() -> Engine:
    engine = create_engine(PG_URL, future=True)
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT 1"))
        except Exception:
            pytest.skip("PostgreSQL not reachable at TEST_POSTGRES_URL")
    ensure_schema(engine)
    return engine
