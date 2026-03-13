"""Tests for AspectService — version-0 pattern, MCL, compaction (Issue #2929)."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nexus.contracts.aspects import (
    AspectRegistry,
    OwnershipAspect,
    PathAspect,
    SchemaMetadataAspect,
)
from nexus.storage.aspect_service import AspectService
from nexus.storage.models._base import Base
from nexus.storage.models.aspect_store import EntityAspectModel
from nexus.storage.models.metadata_change_log import MetadataChangeLogModel


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database with tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset and re-register aspects for each test."""
    AspectRegistry.reset()
    registry = AspectRegistry.get()
    registry.register("path", PathAspect, max_versions=5)
    registry.register("schema_metadata", SchemaMetadataAspect, max_versions=20)
    registry.register("ownership", OwnershipAspect, max_versions=5)
    yield
    AspectRegistry.reset()


class TestAspectServiceCRUD:
    """Basic CRUD operations."""

    def test_put_and_get_new_aspect(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect(
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            payload={"virtual_path": "/data/file.csv"},
            created_by="alice",
        )
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/data/file.csv"

    def test_get_nonexistent_returns_none(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        assert svc.get_aspect("urn:nexus:file:z1:nope", "path") is None

    def test_put_updates_existing(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v2"})
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/v2"

    def test_delete_aspect(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        deleted = svc.delete_aspect("urn:nexus:file:z1:id1", "path")
        db_session.commit()

        assert deleted is True
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_delete_nonexistent_returns_false(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        assert svc.delete_aspect("urn:nexus:file:z1:nope", "path") is False

    def test_list_aspects(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        svc.put_aspect(
            "urn:nexus:file:z1:id1", "ownership", {"owner_id": "alice", "owner_type": "user"}
        )
        db_session.commit()

        names = svc.list_aspects("urn:nexus:file:z1:id1")
        assert sorted(names) == ["ownership", "path"]


class TestVersion0Pattern:
    """Version-0 swap pattern tests."""

    def test_first_write_creates_version_0(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        version = svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        assert version == 0  # First write, no history created

        # Verify version 0 exists
        v0 = svc.get_aspect_version("urn:nexus:file:z1:id1", "path", 0)
        assert v0 is not None
        assert v0["virtual_path"] == "/v1"

    def test_update_creates_history(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        version = svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v2"})
        db_session.commit()

        assert version > 0  # History version created

        # Version 0 is the latest
        current = svc.get_aspect_version("urn:nexus:file:z1:id1", "path", 0)
        assert current is not None
        assert current["virtual_path"] == "/v2"

        # Historical version preserves old value
        history = svc.get_aspect_version("urn:nexus:file:z1:id1", "path", version)
        assert history is not None
        assert history["virtual_path"] == "/v1"

    def test_aspect_history(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        for i in range(5):
            svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": f"/v{i}"})
            db_session.commit()

        history = svc.get_aspect_history("urn:nexus:file:z1:id1", "path")
        assert len(history) == 5  # version 0 + 4 history versions
        # Version 0 (current) has latest value; history versions ordered desc
        current = [h for h in history if h["version"] == 0]
        assert len(current) == 1
        assert current[0]["payload"]["virtual_path"] == "/v4"


class TestInlineCompaction:
    """Inline compaction tests (bounded version history)."""

    def test_compaction_limits_history(self, db_session: Session) -> None:
        """Path aspect has max_versions=5. After 8 writes, only 5+1 versions remain."""
        svc = AspectService(db_session)
        for i in range(8):
            svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": f"/v{i}"})
            db_session.commit()

        # Count active versions
        stmt = select(EntityAspectModel).where(
            EntityAspectModel.entity_urn == "urn:nexus:file:z1:id1",
            EntityAspectModel.aspect_name == "path",
            EntityAspectModel.deleted_at.is_(None),
        )
        rows = db_session.execute(stmt).scalars().all()
        # version 0 (current) + max 5 history = 6 max
        assert len(rows) <= 6


class TestBatchLoading:
    """Batch aspect loading (N+1 prevention)."""

    def test_batch_load(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        urns = [f"urn:nexus:file:z1:id{i}" for i in range(5)]
        for urn in urns:
            svc.put_aspect(urn, "path", {"virtual_path": f"/{urn}"})
        db_session.commit()

        result = svc.get_aspects_batch(urns, "path")
        assert len(result) == 5
        for urn in urns:
            assert urn in result

    def test_batch_load_partial(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        result = svc.get_aspects_batch(
            ["urn:nexus:file:z1:id1", "urn:nexus:file:z1:id2"],
            "path",
        )
        assert len(result) == 1
        assert "urn:nexus:file:z1:id1" in result

    def test_batch_load_empty(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        assert svc.get_aspects_batch([], "path") == {}


class TestSoftDeleteCascade:
    """Soft-delete cascade on entity deletion."""

    def test_soft_delete_all_aspects(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        urn = "urn:nexus:file:z1:id1"
        svc.put_aspect(urn, "path", {"virtual_path": "/v1"})
        svc.put_aspect(urn, "ownership", {"owner_id": "alice", "owner_type": "user"})
        db_session.commit()

        count = svc.soft_delete_entity_aspects(urn)
        db_session.commit()

        assert count >= 2
        assert svc.get_aspect(urn, "path") is None
        assert svc.get_aspect(urn, "ownership") is None


class TestMCLRecording:
    """MCL records are created for aspect changes."""

    def test_put_creates_mcl_upsert(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"}, zone_id="z1")
        db_session.commit()

        mcl_records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        assert len(mcl_records) == 1
        assert mcl_records[0].change_type == "upsert"
        assert mcl_records[0].entity_urn == "urn:nexus:file:z1:id1"
        assert mcl_records[0].aspect_name == "path"

    def test_delete_creates_mcl_delete(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        svc.delete_aspect("urn:nexus:file:z1:id1", "path")
        db_session.commit()

        mcl_records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        # 1 upsert + 1 delete
        assert len(mcl_records) == 2
        assert mcl_records[1].change_type == "delete"

    def test_mcl_sequence_monotonic(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        for i in range(5):
            svc.put_aspect(f"urn:nexus:file:z1:id{i}", "path", {"virtual_path": f"/v{i}"})
        db_session.commit()

        mcl_records = (
            db_session.execute(
                select(MetadataChangeLogModel).order_by(MetadataChangeLogModel.sequence_number)
            )
            .scalars()
            .all()
        )

        sequences = [r.sequence_number for r in mcl_records]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)  # All unique

    def test_mcl_contains_previous_value(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v2"})
        db_session.commit()

        mcl_records = (
            db_session.execute(
                select(MetadataChangeLogModel).order_by(MetadataChangeLogModel.sequence_number)
            )
            .scalars()
            .all()
        )

        # Second MCL record should have previous_value
        assert len(mcl_records) == 2
        prev = json.loads(mcl_records[1].previous_value)
        assert prev["virtual_path"] == "/v1"


class TestValidation:
    """Input validation tests."""

    def test_unknown_aspect_rejected(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        with pytest.raises(ValueError, match="Unknown aspect type"):
            svc.put_aspect("urn:nexus:file:z1:id1", "nonexistent", {"key": "val"})

    def test_oversized_payload_rejected(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        with pytest.raises(ValueError, match="exceeds"):
            svc.put_aspect(
                "urn:nexus:file:z1:id1",
                "path",
                {"virtual_path": "x" * 2_000_000},
            )
