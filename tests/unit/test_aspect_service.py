"""Tests for AspectService — version-0 pattern, MCL, compaction (Issue #2929)."""

import json

import pytest
from sqlalchemy import create_engine, func, select
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
from nexus.storage.models.operation_log import OperationLogModel


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
    """MCL records are created in operation_log for aspect changes (Key Decision #2)."""

    @staticmethod
    def _mcl_rows(session: Session) -> list[OperationLogModel]:
        """Return operation_log rows that carry MCL semantics."""
        return list(
            session.execute(
                select(OperationLogModel)
                .where(OperationLogModel.entity_urn.isnot(None))
                .order_by(OperationLogModel.sequence_number)
            )
            .scalars()
            .all()
        )

    def test_put_creates_mcl_upsert(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"}, zone_id="z1")
        db_session.commit()

        rows = self._mcl_rows(db_session)
        assert len(rows) == 1
        assert rows[0].change_type == "upsert"
        assert rows[0].entity_urn == "urn:nexus:file:z1:id1"
        assert rows[0].aspect_name == "path"
        assert rows[0].operation_type == "aspect_upsert"

    def test_delete_creates_mcl_delete(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        svc.delete_aspect("urn:nexus:file:z1:id1", "path")
        db_session.commit()

        rows = self._mcl_rows(db_session)
        # 1 upsert + 1 delete
        assert len(rows) == 2
        assert rows[1].change_type == "delete"
        assert rows[1].operation_type == "aspect_delete"

    def test_mcl_sequence_monotonic(self, db_session: Session) -> None:
        svc = AspectService(db_session)
        for i in range(5):
            svc.put_aspect(f"urn:nexus:file:z1:id{i}", "path", {"virtual_path": f"/v{i}"})
        db_session.commit()

        rows = self._mcl_rows(db_session)
        sequences = [r.sequence_number for r in rows]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)  # All unique

    def test_mcl_contains_aspect_value(self, db_session: Session) -> None:
        """MCL rows carry the new aspect value in metadata_snapshot."""
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v2"})
        db_session.commit()

        rows = self._mcl_rows(db_session)
        assert len(rows) == 2
        # Second MCL record carries the new value (not previous)
        snapshot = json.loads(rows[1].metadata_snapshot)
        assert snapshot["virtual_path"] == "/v2"


class TestRecordMclFlag:
    """Tests for record_mcl=False (reindex replay support)."""

    @staticmethod
    def _mcl_count(session: Session) -> int:
        """Count operation_log rows with MCL semantics."""
        return session.execute(
            select(func.count())
            .select_from(OperationLogModel)
            .where(OperationLogModel.entity_urn.isnot(None))
        ).scalar_one()

    def test_put_aspect_skip_mcl(self, db_session: Session) -> None:
        """put_aspect(record_mcl=False) writes aspect but no MCL row."""
        svc = AspectService(db_session)
        svc.put_aspect(
            "urn:nexus:file:z1:id1",
            "path",
            {"virtual_path": "/v1"},
            record_mcl=False,
        )
        db_session.commit()

        # Aspect should exist
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is not None

        # But no MCL row should have been created in operation_log
        assert self._mcl_count(db_session) == 0

    def test_delete_aspect_skip_mcl(self, db_session: Session) -> None:
        """delete_aspect(record_mcl=False) deletes aspect but no MCL row."""
        svc = AspectService(db_session)
        svc.put_aspect(
            "urn:nexus:file:z1:id1",
            "path",
            {"virtual_path": "/v1"},
            record_mcl=False,
        )
        db_session.commit()

        svc.delete_aspect("urn:nexus:file:z1:id1", "path", record_mcl=False)
        db_session.commit()

        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

        assert self._mcl_count(db_session) == 0


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
