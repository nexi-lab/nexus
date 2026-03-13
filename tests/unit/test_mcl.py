"""Tests for MCL recorder — metadata change log integration (Issue #2929)."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.storage.mcl_recorder import MCLRecorder
from nexus.storage.models._base import Base
from nexus.storage.models.metadata_change_log import MCLChangeType, MetadataChangeLogModel


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database with MCL table."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


class TestMCLRecorder:
    """MCL recorder integration tests."""

    def test_record_file_write(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z1:id1",
            metadata_dict={"path": "/data/file.csv", "size": 1024},
            zone_id="z1",
            changed_by="alice",
        )
        db_session.commit()

        records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        assert len(records) == 1
        assert records[0].change_type == MCLChangeType.UPSERT.value
        assert records[0].entity_urn == "urn:nexus:file:z1:id1"
        assert records[0].zone_id == "z1"

        value = json.loads(records[0].aspect_value)
        assert value["path"] == "/data/file.csv"

    def test_record_file_delete(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        recorder.record_file_delete(
            entity_urn="urn:nexus:file:z1:id1",
            zone_id="z1",
            previous_metadata={"path": "/data/file.csv"},
        )
        db_session.commit()

        records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        assert len(records) == 1
        assert records[0].change_type == MCLChangeType.DELETE.value

    def test_record_file_rename(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        recorder.record_file_rename(
            entity_urn="urn:nexus:file:z1:id1",
            old_path="/data/old.csv",
            new_path="/data/new.csv",
            zone_id="z1",
        )
        db_session.commit()

        records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        assert len(records) == 1
        assert records[0].change_type == MCLChangeType.PATH_CHANGED.value

        value = json.loads(records[0].aspect_value)
        assert value["virtual_path"] == "/data/new.csv"

        prev = json.loads(records[0].previous_value)
        assert prev["virtual_path"] == "/data/old.csv"

    def test_sequence_numbers_monotonic(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        for i in range(10):
            recorder.record_file_write(
                entity_urn=f"urn:nexus:file:z1:id{i}",
                metadata_dict={"path": f"/file{i}"},
            )
        db_session.commit()

        records = (
            db_session.execute(
                select(MetadataChangeLogModel).order_by(MetadataChangeLogModel.sequence_number)
            )
            .scalars()
            .all()
        )

        sequences = [r.sequence_number for r in records]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    def test_failure_does_not_raise(self, db_session) -> None:
        """MCL recorder failures are swallowed (non-critical)."""
        recorder = MCLRecorder(db_session)
        # Close the session to force a failure
        db_session.close()

        # Should not raise
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z1:id1",
            metadata_dict={"path": "/file"},
        )


class TestMCLIdempotency:
    """MCL replay idempotency tests (Issue #2929, Test Review #9)."""

    def test_replay_same_upsert_twice_produces_same_result(self, db_session) -> None:
        """Replaying the same MCL upsert sequence should be idempotent."""
        from nexus.contracts.aspects import AspectRegistry, PathAspect
        from nexus.storage.aspect_service import AspectService

        AspectRegistry.reset()
        AspectRegistry.get().register("path", PathAspect, max_versions=5)

        svc = AspectService(db_session)

        # First apply
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        result_1 = svc.get_aspect("urn:nexus:file:z1:id1", "path")

        # "Replay" by applying the same value again
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()

        result_2 = svc.get_aspect("urn:nexus:file:z1:id1", "path")

        # Same current state
        assert result_1 == result_2

        AspectRegistry.reset()


class TestMCLChangeType:
    """MCLChangeType enum tests."""

    def test_values(self) -> None:
        assert MCLChangeType.UPSERT.value == "upsert"
        assert MCLChangeType.DELETE.value == "delete"
        assert MCLChangeType.PATH_CHANGED.value == "path_changed"
