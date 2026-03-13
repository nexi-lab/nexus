"""Tests for reindex CLI _MCLProcessor — replay MCL to rebuild aspect store (Issue #2929)."""

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
from nexus.storage.models.metadata_change_log import MCLChangeType, MetadataChangeLogModel


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


class TestMCLProcessorRebuildsAspectStore:
    """Verify that _MCLProcessor actually mutates the aspect store."""

    def test_upsert_creates_aspect(self, db_session) -> None:
        """UPSERT MCL record creates an aspect via put_aspect."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        # Create a mock MCL record
        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.UPSERT.value,
            aspect_value=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )

        proc.process(mcl)
        db_session.commit()

        # Verify the aspect was actually written to the store
        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/data/file.csv"

    def test_delete_removes_aspect(self, db_session) -> None:
        """DELETE MCL record soft-deletes the aspect."""
        svc = AspectService(db_session)

        # First create the aspect
        svc.put_aspect(
            "urn:nexus:file:z1:id1", "path", {"virtual_path": "/data/file.csv"}, zone_id="z1"
        )
        db_session.commit()

        # Verify it exists
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is not None

        # Now process a DELETE MCL record
        proc = _make_processor(db_session, target="search")
        mcl = MetadataChangeLogModel(
            sequence_number=2,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.DELETE.value,
            zone_id="z1",
        )

        proc.process(mcl)
        db_session.commit()

        # Verify the aspect was soft-deleted
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_path_changed_updates_path_aspect(self, db_session) -> None:
        """PATH_CHANGED MCL record updates the path aspect."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.PATH_CHANGED.value,
            aspect_value=json.dumps({"virtual_path": "/data/renamed.csv"}),
            zone_id="z1",
        )

        proc.process(mcl)
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/data/renamed.csv"

    def test_upsert_with_null_aspect_value_skipped(self, db_session) -> None:
        """UPSERT with no aspect_value is a no-op."""
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.UPSERT.value,
            aspect_value=None,
            zone_id="z1",
        )

        proc.process(mcl)
        db_session.commit()

        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_versions_target_builds_history(self, db_session) -> None:
        """Versions target replays upserts, building version history."""
        proc = _make_processor(db_session, target="versions")
        svc = AspectService(db_session)

        # Replay multiple upserts for the same entity
        for i in range(3):
            mcl = MetadataChangeLogModel(
                sequence_number=i + 1,
                entity_urn="urn:nexus:file:z1:id1",
                aspect_name="path",
                change_type=MCLChangeType.UPSERT.value,
                aspect_value=json.dumps({"virtual_path": f"/data/v{i}.csv"}),
                zone_id="z1",
            )
            proc.process(mcl)

        db_session.commit()

        # Verify version history was built
        history = svc.get_aspect_history("urn:nexus:file:z1:id1", "path")
        assert len(history) == 3  # v0 (current) + 2 history versions

        # Current (v0) should have the latest value
        current = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert current is not None
        assert current["virtual_path"] == "/data/v2.csv"

    def test_all_target_runs_both_search_and_versions(self, db_session) -> None:
        """Target 'all' processes for both search and versions."""
        proc = _make_processor(db_session, target="all")
        assert proc._targets == {"search", "versions"}

        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.UPSERT.value,
            aspect_value=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )

        proc.process(mcl)
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

        # Seed one MCL row
        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="path",
            change_type=MCLChangeType.UPSERT.value,
            aspect_value=json.dumps({"virtual_path": "/data/file.csv"}),
            zone_id="z1",
        )
        db_session.add(mcl)
        db_session.commit()

        # Count MCL rows before replay
        count_before = db_session.execute(
            select(func.count()).select_from(MetadataChangeLogModel)
        ).scalar_one()

        # Process the MCL row
        proc.process(mcl)
        db_session.commit()

        # Count MCL rows after replay — should be unchanged
        count_after = db_session.execute(
            select(func.count()).select_from(MetadataChangeLogModel)
        ).scalar_one()

        assert count_after == count_before, (
            f"Replay generated {count_after - count_before} new MCL row(s). "
            "record_mcl=False should prevent self-amplification."
        )

    def test_file_metadata_aspect_replayable(self, db_session) -> None:
        """file_metadata MCL rows (from MCLRecorder) must be replayable.

        Regression test: MCLRecorder emits aspect_name='file_metadata' for
        write/delete events, but the registry previously didn't register it,
        causing ValueError('Unknown aspect type') during reindex.
        """
        proc = _make_processor(db_session, target="search")
        svc = AspectService(db_session)

        # Simulate a file_metadata MCL row as MCLRecorder would emit
        mcl = MetadataChangeLogModel(
            sequence_number=1,
            entity_urn="urn:nexus:file:z1:id1",
            aspect_name="file_metadata",
            change_type=MCLChangeType.UPSERT.value,
            aspect_value=json.dumps(
                {
                    "path": "/data/file.csv",
                    "size": 1024,
                    "etag": "abc123",
                }
            ),
            zone_id="z1",
        )

        # Should NOT raise ValueError — file_metadata is now registered
        proc.process(mcl)
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "file_metadata")
        assert result is not None
        assert result["path"] == "/data/file.csv"

    def test_end_to_end_replay_via_mcl_recorder(self, db_session) -> None:
        """Full round-trip: record MCL → replay → verify aspect store state."""
        from nexus.storage.mcl_recorder import MCLRecorder

        svc = AspectService(db_session)

        # First create some aspects (which writes MCL records)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/a.csv"}, zone_id="z1")
        svc.put_aspect("urn:nexus:file:z1:id2", "path", {"virtual_path": "/b.csv"}, zone_id="z1")
        db_session.commit()

        # Delete them (simulating a fresh reindex from scratch)
        svc.soft_delete_entity_aspects("urn:nexus:file:z1:id1")
        svc.soft_delete_entity_aspects("urn:nexus:file:z1:id2")
        db_session.commit()

        # Verify they're gone
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None
        assert svc.get_aspect("urn:nexus:file:z1:id2", "path") is None

        # Replay MCL to rebuild
        recorder = MCLRecorder(db_session)
        proc = _make_processor(db_session, target="search")
        for mcl in recorder.replay_changes():
            if mcl.change_type == MCLChangeType.UPSERT.value:
                proc.process(mcl)
        db_session.commit()

        # Verify aspects were rebuilt
        result1 = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result1 is not None
        assert result1["virtual_path"] == "/a.csv"

        result2 = svc.get_aspect("urn:nexus:file:z1:id2", "path")
        assert result2 is not None
        assert result2["virtual_path"] == "/b.csv"
