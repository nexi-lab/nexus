"""Tests for schema invariant repair helpers."""

from sqlalchemy import create_engine

from nexus.storage.models._base import Base
from nexus.storage.schema_invariants import ensure_postgres_schema_invariants


def test_postgres_schema_invariants_noop_for_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    ensure_postgres_schema_invariants(engine)
