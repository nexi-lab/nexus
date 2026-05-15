"""Tests for reindex CLI _MCLProcessor — replay operation_log MCL to rebuild aspect store (Issue #2929)."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.aspects import (
    AspectRegistry,
    FileMetadataAspect,
    PathAspect,
    SchemaMetadataAspect,
)
from nexus.storage.aspect_service import AspectService
from nexus.storage.models._base import Base
from nexus.storage.models.operation_log import OperationLogModel


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database with all tables."""
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
    registry.register("file_metadata", FileMetadataAspect, max_versions=10)
    yield
    AspectRegistry.reset()


def _make_processor(session, target="all"):
    """Create a _MCLProcessor for testing."""
    from nexus.cli.commands.reindex import _MCLProcessor

    return _MCLProcessor(session, target)


def _make_mcl_row(**kwargs):
    """Create an OperationLogModel row with MCL columns populated.

    Provides defaults for required operation_log fields (operation_type,
    path, status) so tests only need to specify MCL-relevant fields.
    """
    defaults = {
        "operation_type": "write",
        "path": "/data/file.csv",
        "status": "success",
    }
    defaults.update(kwargs)
    return OperationLogModel(**defaults)


class TestMCLProcessorRebuildsAspectStore:
    """Verify that _MCLProcessor actually mutates the aspect store."""

    def test_upsert_creates_aspect(self, db_session) -> None:
        """UPSERT MCL record creates an aspect via put_aspect."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/data/file.csv"

    def test_delete_removes_aspect(self, db_session) -> None:
        """DELETE MCL record soft-deletes the aspect."""
        svc = AspectService(db_session)

        svc.put_aspect(
            "urn:nexus:file:z1:id1", "path", {"virtual_path": "/data/file.csv"}, zone_id="z1"
        )
        db_session.commit()

        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is not None

        proc = _make_processor(db_session, target="search")
        row = _make_mcl_row(
            sequence_number=2,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="delete",
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_path_changed_updates_path_aspect(self, db_session) -> None:
        """PATH_CHANGED MCL record updates the path aspect."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="path_changed",
            metadata_snapshot=json.dumps({"virtual_path": "/data/renamed.csv"}),
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/data/renamed.csv"

    def test_upsert_with_null_metadata_snapshot_skipped(self, db_session) -> None:
        """UPSERT with no metadata_snapshot is a no-op."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot=None,
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_versions_target_builds_history(self, db_session) -> None:
        """Versions target replays upserts, building version history."""
        proc = _make_processor(db_session, target="versions")
        svc = AspectService(db_session)

        for i in range(3):
            row = _make_mcl_row(
                sequence_number=i + 1,
                entity_urn="urn:nexus:file:z1:id1",
                aspect_name="path",
                change_type="upsert",
                metadata_snapshot=json.dumps({"virtual_path": f"/data/v{i}.csv"}),
                zone_id="z1",
            )
            proc.process(row)

        db_session.commit()

        history = svc.get_aspect_history("urn:nexus:file:z1:id1", "path")
        assert len(history) == 3

        current = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert current is not None
        assert current["virtual_path"] == "/data/v2.csv"

    def test_all_target_runs_both_search_and_versions(self, db_session) -> None:
        """Target 'all' processes for both search and versions."""
        proc = _make_processor(db_session, target="all")
        assert proc._targets == {"search", "versions"}

        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        svc = AspectService(db_session)
        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None

    def test_replay_does_not_generate_new_mcl_rows(self, db_session) -> None:
        """Reindex replay must not self-amplify by generating new MCL rows.

        Regression test: previously, _MCLProcessor called put_aspect() which
        wrote new MCL rows into the same table being iterated, causing the
        replay stream to grow unboundedly.
        """
        from sqlalchemy import func, select

        proc = _make_processor(db_session, target="search")

        # Seed one operation_log row with MCL columns
        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )
        db_session.add(row)
        db_session.commit()

        # Count operation_log MCL rows before replay
        count_before = db_session.execute(
            select(func.count())
            .select_from(OperationLogModel)
            .where(OperationLogModel.entity_urn.isnot(None))
        ).scalar_one()

        proc.process(row)
        db_session.commit()

        # Count MCL rows after replay — should be unchanged
        count_after = db_session.execute(
            select(func.count())
            .select_from(OperationLogModel)
            .where(OperationLogModel.entity_urn.isnot(None))
        ).scalar_one()

        assert count_after == count_before, (
            f"Replay generated {count_after - count_before} new MCL row(s). "
            "record_mcl=False should prevent self-amplification."
        )

    def test_file_metadata_aspect_replayable(self, db_session) -> None:
        """file_metadata MCL rows must be replayable.

        Regression test: MCL rows emit aspect_name='file_metadata' for
        write/delete events, but the registry previously didn't register it,
        causing ValueError('Unknown aspect type') during reindex.
        """
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        row = _make_mcl_row(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="file_metadata",
            change_type="upsert",
            metadata_snapshot=json.dumps(
                {
                    "path": "/data/file.csv",
                    "size": 1024,
                    "content_id": "abc123",
                }
            ),
            zone_id="z1",
        )

        proc.process(row)
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "file_metadata")
        assert result is not None
        assert result["path"] == "/data/file.csv"

    def test_end_to_end_replay_via_operation_logger(self, db_session) -> None:
        """Full round-trip: log operations → replay → verify aspect store state.

        Uses OperationLogger (Key Decision #2: MCL in operation_log) as the
        single replay source, not MCLRecorder.
        """
        from nexus.storage.operation_logger import OperationLogger

        svc = AspectService(db_session)

        # Log operations with MCL columns via OperationLogger
        op_logger = OperationLogger(db_session)
        op_logger.log_operation(
            operation_type="write",
            path="/a.csv",
            zone_id="z1",
            status="success",
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot={"virtual_path": "/a.csv"},
        )
        op_logger.log_operation(
            operation_type="write",
            path="/b.csv",
            zone_id="z1",
            status="success",
            entity_urn="urn:nexus:file:z1:id2",
            aspect_name="path",
            change_type="upsert",
            metadata_snapshot={"virtual_path": "/b.csv"},
        )
        db_session.commit()

        # Replay via OperationLogger.replay_changes()
        proc = _make_processor(db_session, target="search")
        for row in op_logger.replay_changes():
            proc.process(row)
        db_session.commit()

        # Verify aspects were rebuilt
        result1 = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result1 is not None
        assert result1["virtual_path"] == "/a.csv"

        result2 = svc.get_aspect("urn:nexus:file:z1:id2", "path")
        assert result2 is not None
        assert result2["virtual_path"] == "/b.csv"
