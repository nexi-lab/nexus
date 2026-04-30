"""Tests for MCL recorder — metadata change log integration (Issue #2929)."""

import json
from types import SimpleNamespace
from typing import Any

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


class TestMCLSequenceAllocation:
    """Issue #3062: Sequence allocation should produce unique, monotonic values."""

    def test_postgres_sequence_number_allocated_before_flush(self) -> None:
        """PostgreSQL inserts should not rely on a server default SQLAlchemy omits."""

        class _FakeScalarResult:
            def __init__(self, value: int) -> None:
                self._value = value

            def scalar_one(self) -> int:
                return self._value

        class _FakePostgresSession:
            bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            def __init__(self) -> None:
                self.executed: list[str] = []
                self.added: list[MetadataChangeLogModel] = []
                self.flushed = False

            def execute(self, statement: Any) -> _FakeScalarResult:
                self.executed.append(str(statement))
                return _FakeScalarResult(42)

            def add(self, obj: MetadataChangeLogModel) -> None:
                self.added.append(obj)

            def flush(self) -> None:
                self.flushed = True

            def rollback(self) -> None:
                raise AssertionError("rollback should not be called")

        session = _FakePostgresSession()

        MCLRecorder(session).record_file_write(
            entity_urn="urn:nexus:file:root:id1",
            metadata_dict={"path": "/workspace/demo/plan.md"},
            zone_id="root",
        )

        assert session.flushed
        assert session.added[0].sequence_number == 42
        assert "nextval('mcl_sequence_number_seq')" in session.executed[0]

    def test_multiple_writes_get_unique_sequences(self, db_session) -> None:
        """Concurrent-style writes should each get a unique sequence number."""
        recorder = MCLRecorder(db_session)
        for i in range(20):
            recorder.record_file_write(
                entity_urn=f"urn:nexus:file:z1:id{i}",
                metadata_dict={"path": f"/file{i}"},
            )
        db_session.commit()

        records = db_session.execute(select(MetadataChangeLogModel)).scalars().all()
        sequences = [r.sequence_number for r in records]
        assert len(sequences) == 20
        assert len(set(sequences)) == 20, "All sequence numbers must be unique"

    def test_sequence_numbers_increase(self, db_session) -> None:
        """Sequence numbers should monotonically increase."""
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
        for i in range(1, len(sequences)):
            assert sequences[i] > sequences[i - 1]

    def test_replay_handles_gaps(self, db_session) -> None:
        """replay_changes() should work even if there are gaps in sequence numbers."""
        recorder = MCLRecorder(db_session)
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z1:id1",
            metadata_dict={"path": "/a"},
        )
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z1:id2",
            metadata_dict={"path": "/b"},
        )
        db_session.commit()

        # Delete the first record to create a gap
        first = (
            db_session.execute(
                select(MetadataChangeLogModel).order_by(MetadataChangeLogModel.sequence_number)
            )
            .scalars()
            .first()
        )
        db_session.delete(first)
        db_session.commit()

        # Replay should still work with the remaining record
        replayed = list(recorder.replay_changes())
        assert len(replayed) == 1


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


class TestMCLReplay:
    """MCL replay_changes() iterator tests."""

    def test_replay_changes_returns_all_records(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        for i in range(5):
            recorder.record_file_write(
                entity_urn=f"urn:nexus:file:z1:id{i}",
                metadata_dict={"path": f"/file{i}"},
                zone_id="z1",
            )
        db_session.commit()

        replayed = list(recorder.replay_changes())
        assert len(replayed) == 5

    def test_replay_changes_filters_by_zone(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z1:id1",
            metadata_dict={"path": "/a"},
            zone_id="z1",
        )
        recorder.record_file_write(
            entity_urn="urn:nexus:file:z2:id2",
            metadata_dict={"path": "/b"},
            zone_id="z2",
        )
        db_session.commit()

        replayed = list(recorder.replay_changes(zone_id="z1"))
        assert len(replayed) == 1
        assert replayed[0].zone_id == "z1"

    def test_replay_changes_from_sequence(self, db_session) -> None:
        recorder = MCLRecorder(db_session)
        for i in range(5):
            recorder.record_file_write(
                entity_urn=f"urn:nexus:file:z1:id{i}",
                metadata_dict={"path": f"/file{i}"},
            )
        db_session.commit()

        all_records = list(recorder.replay_changes())
        mid_seq = all_records[2].sequence_number

        replayed = list(recorder.replay_changes(from_sequence=mid_seq))
        assert len(replayed) == 3


class TestURNLocator:
    """Tests for URN locator pattern (Issue #2929 Key Decision #3).

    URNs are locators derived from path via SHA-256 hash — no database
    lookups needed. They change on rename.
    """

    def test_build_urn_deterministic_hash(self) -> None:
        """_build_urn produces a deterministic SHA-256 hash from path."""
        import hashlib

        from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

        urn = RecordStoreWriteObserver._build_urn("/data/file.csv", "z1")
        expected_hash = hashlib.sha256(b"/data/file.csv").hexdigest()[:32]
        assert urn == f"urn:nexus:file:z1:{expected_hash}"

    def test_build_urn_default_zone(self) -> None:
        """When zone_id is None, uses 'default' in URN."""
        import hashlib

        from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

        urn = RecordStoreWriteObserver._build_urn("/data/file.csv", None)
        expected_hash = hashlib.sha256(b"/data/file.csv").hexdigest()[:32]
        assert urn == f"urn:nexus:file:default:{expected_hash}"

    def test_build_urn_changes_on_rename(self) -> None:
        """URNs are locators: different path → different URN."""
        from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

        urn_old = RecordStoreWriteObserver._build_urn("/data/old.csv", "z1")
        urn_new = RecordStoreWriteObserver._build_urn("/data/new.csv", "z1")
        assert urn_old != urn_new

    def test_build_urn_same_path_same_result(self) -> None:
        """Same path always produces the same URN (deterministic)."""
        from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

        urn1 = RecordStoreWriteObserver._build_urn("/data/file.csv", "z1")
        urn2 = RecordStoreWriteObserver._build_urn("/data/file.csv", "z1")
        assert urn1 == urn2


class TestMCLChangeType:
    """MCLChangeType enum tests."""

    def test_values(self) -> None:
        assert MCLChangeType.UPSERT.value == "upsert"
        assert MCLChangeType.DELETE.value == "delete"
        assert MCLChangeType.PATH_CHANGED.value == "path_changed"


class TestOperationLogMCLColumns:
    """Tests for MCL columns on operation_log (Issue #2929 Step 4)."""

    def test_log_operation_with_mcl_fields(self, db_session) -> None:
        """log_operation() accepts and stores entity_urn, aspect_name, change_type."""
        from nexus.storage.operation_logger import OperationLogger

        logger = OperationLogger(db_session)
        op_id = logger.log_operation(
            operation_type="write",
            path="/data/file.csv",
            zone_id="z1",
            status="success",
            entity_urn="urn:nexus:file:z1:abc123",
            aspect_name="file_metadata",
            change_type="upsert",
        )
        db_session.commit()

        op = logger.get_operation(op_id)
        assert op is not None
        assert op.entity_urn == "urn:nexus:file:z1:abc123"
        assert op.aspect_name == "file_metadata"
        assert op.change_type == "upsert"

    def test_log_operation_mcl_fields_nullable(self, db_session) -> None:
        """MCL fields are optional — NULL for legacy operations."""
        from nexus.storage.operation_logger import OperationLogger

        logger = OperationLogger(db_session)
        op_id = logger.log_operation(
            operation_type="write",
            path="/data/file.csv",
            status="success",
        )
        db_session.commit()

        op = logger.get_operation(op_id)
        assert op is not None
        assert op.entity_urn is None
        assert op.aspect_name is None
        assert op.change_type is None

    def test_replay_changes_yields_mcl_rows(self, db_session) -> None:
        """replay_changes() yields only rows where entity_urn IS NOT NULL."""
        from nexus.storage.operation_logger import OperationLogger

        logger = OperationLogger(db_session)

        # Row without MCL fields (legacy)
        logger.log_operation(
            operation_type="mkdir",
            path="/data",
            status="success",
        )

        # Row with MCL fields
        logger.log_operation(
            operation_type="write",
            path="/data/file.csv",
            zone_id="z1",
            status="success",
            entity_urn="urn:nexus:file:z1:abc123",
            aspect_name="file_metadata",
            change_type="upsert",
        )
        db_session.commit()

        replayed = list(logger.replay_changes())
        assert len(replayed) == 1
        assert replayed[0].entity_urn == "urn:nexus:file:z1:abc123"

    def test_replay_changes_filters_by_zone(self, db_session) -> None:
        """replay_changes() respects zone_id filter."""
        from nexus.storage.operation_logger import OperationLogger

        logger = OperationLogger(db_session)

        logger.log_operation(
            operation_type="write",
            path="/a",
            zone_id="z1",
            status="success",
            entity_urn="urn:nexus:file:z1:aaa",
            aspect_name="file_metadata",
            change_type="upsert",
        )
        logger.log_operation(
            operation_type="write",
            path="/b",
            zone_id="z2",
            status="success",
            entity_urn="urn:nexus:file:z2:bbb",
            aspect_name="file_metadata",
            change_type="upsert",
        )
        db_session.commit()

        replayed = list(logger.replay_changes(zone_id="z1"))
        assert len(replayed) == 1
        assert replayed[0].zone_id == "z1"
