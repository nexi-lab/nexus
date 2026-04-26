"""Issue #3897 — every schema-bootstrap path seeds zones.root.

Covers the non-FastAPI surfaces that build the schema via
``Base.metadata.create_all`` (CLI tooling, tests, ``nexus hub`` flows):
they must satisfy the ``api_key_zones`` FK without depending on
``startup_permissions()``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.zone_bootstrap import ensure_root_zone


def test_record_store_seeds_root_zone_on_create_tables(tmp_path):
    """SQLAlchemyRecordStore(create_tables=True) leaves zones.root in place."""
    db = tmp_path / "store.db"
    store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db}", create_tables=True)

    with store.session_factory() as session:
        zone = session.get(ZoneModel, ROOT_ZONE_ID)
    assert zone is not None
    assert zone.phase == "Active"
    assert zone.deleted_at is None


def test_create_api_key_via_record_store_without_lifespan(tmp_path):
    """The CI/CLI path: build store, mint a root-scoped key, no FastAPI."""
    db = tmp_path / "store.db"
    store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db}", create_tables=True)

    with store.session_factory() as session:
        key_id, raw = create_api_key(
            session,
            user_id="admin",
            name="agent:test",
            subject_type="agent",
            subject_id="agent-test",
            zone_id=ROOT_ZONE_ID,
        )
        session.commit()

    assert key_id
    assert raw.startswith("sk-")


def test_ensure_root_zone_idempotent_across_calls(tmp_path):
    """Re-running ensure_root_zone on an already-seeded store is a no-op."""
    db = tmp_path / "store.db"
    factory = sessionmaker(bind=create_engine(f"sqlite:///{db}"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])

    ensure_root_zone(factory)
    with factory() as s:
        s.execute(
            ZoneModel.__table__.update()
            .where(ZoneModel.zone_id == ROOT_ZONE_ID)
            .values(name="marker")
        )
        s.commit()

    ensure_root_zone(factory)
    with factory() as s:
        zone = s.get(ZoneModel, ROOT_ZONE_ID)
    assert zone is not None
    assert zone.name == "marker"  # untouched on the second call


def test_ensure_root_zone_rejects_inactive_phase(tmp_path):
    """Non-Active root row must fail the bootstrap loudly."""
    db = tmp_path / "store.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(ZoneModel(zone_id=ROOT_ZONE_ID, name="root", phase="Terminating"))
        s.commit()

    with pytest.raises(RuntimeError, match="not usable.*Terminating"):
        ensure_root_zone(factory)


def test_ensure_root_zone_skips_when_zones_table_absent(tmp_path):
    """Schema bootstrap is layered — partial-schema envs must not be fatal.

    When the zones table doesn't exist yet (another bootstrap path will
    create it), ensure_root_zone is a no-op rather than raising. This
    keeps fail-closed semantics for real DB faults but tolerates the
    interleavings that exist in dev/test fixtures.
    """
    db = tmp_path / "store.db"
    engine = create_engine(f"sqlite:///{db}")
    # Note: NO Base.metadata.create_all — zones table never created.
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    ensure_root_zone(factory)  # must not raise


def test_ensure_root_zone_rejects_soft_deleted(tmp_path):
    """deleted_at set on root must fail the bootstrap loudly."""
    db = tmp_path / "store.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(
            ZoneModel(
                zone_id=ROOT_ZONE_ID,
                name="root",
                phase="Active",
                deleted_at=datetime.now(UTC),
            )
        )
        s.commit()

    with pytest.raises(RuntimeError, match="not usable.*deleted_at"):
        ensure_root_zone(factory)
