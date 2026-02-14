"""Tests for DatabaseSnapshotLookup and CASManifestReader (Issue #1428).

Integration tests with in-memory SQLite:
- get_snapshot by ID: insert model, query, verify dict shape
- get_snapshot not found: returns None
- get_latest_snapshot: insert 3, verify latest returned
- get_latest no snapshots: returns None
- CASManifestReader: mock backend, verify path list
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.services.context_manifest.executors.snapshot_lookup_db import (
    CASManifestReader,
    DatabaseSnapshotLookup,
)
from nexus.storage.models import Base
from nexus.storage.models.filesystem import WorkspaceSnapshotModel


@pytest.fixture
def db_session_factory():
    """Create in-memory SQLite database with schema."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory


@pytest.fixture
def mock_record_store(db_session_factory):  # noqa: ARG001
    """Deprecated: kept for backward compat. Use db_session_factory directly."""
    from nexus.storage.record_store import SQLAlchemyRecordStore

    store = MagicMock(spec=SQLAlchemyRecordStore)
    store.session_factory = db_session_factory
    return store


def _insert_snapshot(
    session: Session,
    snapshot_id: str = "snap-001",
    workspace_path: str = "/test-workspace",
    snapshot_number: int = 1,
    manifest_hash: str = "abc123def",
    file_count: int = 10,
    total_size_bytes: int = 5000,
    description: str | None = "Test snapshot",
    created_by: str | None = "user1",
    tags: str | None = None,
    created_at: datetime | None = None,
) -> WorkspaceSnapshotModel:
    """Helper to insert a snapshot model into the test DB."""
    model = WorkspaceSnapshotModel(
        snapshot_id=snapshot_id,
        workspace_path=workspace_path,
        snapshot_number=snapshot_number,
        manifest_hash=manifest_hash,
        file_count=file_count,
        total_size_bytes=total_size_bytes,
        description=description,
        created_by=created_by,
        tags=tags,
        created_at=created_at or datetime.now(UTC),
    )
    session.add(model)
    session.commit()
    return model


# ---------------------------------------------------------------------------
# DatabaseSnapshotLookup tests
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    def test_get_snapshot_by_id(self, db_session_factory: Any, mock_record_store: Any) -> None:
        """Insert model, query by ID, verify dict shape."""
        with db_session_factory() as session:
            _insert_snapshot(
                session,
                snapshot_id="snap-100",
                workspace_path="/ws",
                snapshot_number=3,
                file_count=25,
                total_size_bytes=100000,
                description="Release v1",
                created_by="admin",
                tags=json.dumps(["release"]),
            )

        lookup = DatabaseSnapshotLookup(session_factory=db_session_factory)
        result = lookup.get_snapshot("snap-100")

        assert result is not None
        assert result["snapshot_id"] == "snap-100"
        assert result["workspace_path"] == "/ws"
        assert result["snapshot_number"] == 3
        assert result["file_count"] == 25
        assert result["total_size_bytes"] == 100000
        assert result["description"] == "Release v1"
        assert result["created_by"] == "admin"
        assert result["tags"] == ["release"]
        assert result["manifest_hash"] == "abc123def"
        assert result["created_at"] is not None

    def test_get_snapshot_not_found(self, db_session_factory: Any) -> None:
        """Query for nonexistent ID returns None."""
        lookup = DatabaseSnapshotLookup(session_factory=db_session_factory)

        result = lookup.get_snapshot("nonexistent")

        assert result is None


class TestGetLatestSnapshot:
    def test_get_latest_returns_most_recent(self, db_session_factory: Any) -> None:
        """Insert 3 snapshots, verify latest (by created_at) is returned."""
        now = datetime.now(UTC)
        with db_session_factory() as session:
            _insert_snapshot(
                session,
                snapshot_id="s1",
                workspace_path="/ws",
                snapshot_number=1,
                created_at=now - timedelta(hours=3),
            )
            _insert_snapshot(
                session,
                snapshot_id="s2",
                workspace_path="/ws",
                snapshot_number=2,
                created_at=now - timedelta(hours=1),
            )
            _insert_snapshot(
                session,
                snapshot_id="s3",
                workspace_path="/ws",
                snapshot_number=3,
                created_at=now,
            )

        lookup = DatabaseSnapshotLookup(session_factory=db_session_factory)
        result = lookup.get_latest_snapshot("/ws")

        assert result is not None
        assert result["snapshot_id"] == "s3"
        assert result["snapshot_number"] == 3

    def test_get_latest_no_snapshots(self, db_session_factory: Any) -> None:
        """No snapshots for workspace → returns None."""
        lookup = DatabaseSnapshotLookup(session_factory=db_session_factory)

        result = lookup.get_latest_snapshot("/empty-workspace")

        assert result is None

    def test_get_latest_filters_by_workspace(self, db_session_factory: Any) -> None:
        """Latest is per-workspace, not global."""
        now = datetime.now(UTC)
        with db_session_factory() as session:
            _insert_snapshot(
                session,
                snapshot_id="ws1-snap",
                workspace_path="/ws1",
                snapshot_number=1,
                created_at=now - timedelta(hours=1),
            )
            _insert_snapshot(
                session,
                snapshot_id="ws2-snap",
                workspace_path="/ws2",
                snapshot_number=1,
                created_at=now,
            )

        lookup = DatabaseSnapshotLookup(session_factory=db_session_factory)

        result = lookup.get_latest_snapshot("/ws1")
        assert result is not None
        assert result["snapshot_id"] == "ws1-snap"


# ---------------------------------------------------------------------------
# CASManifestReader tests
# ---------------------------------------------------------------------------


class TestCASManifestReader:
    def test_reads_file_paths_from_manifest(self) -> None:
        """Read manifest from CAS, extract sorted file paths."""
        manifest_data = {
            "src/utils.py": {"hash": "aaa", "size": 100, "mime_type": "text/x-python"},
            "src/main.py": {"hash": "bbb", "size": 200, "mime_type": "text/x-python"},
            "README.md": {"hash": "ccc", "size": 50, "mime_type": "text/markdown"},
        }
        manifest_bytes = json.dumps(manifest_data).encode("utf-8")

        mock_backend = MagicMock()
        mock_backend.read_content.return_value = manifest_bytes

        reader = CASManifestReader(backend=mock_backend)
        paths = reader.read_file_paths("hash123")

        assert paths is not None
        # Sorted alphabetically
        assert paths == ["README.md", "src/main.py", "src/utils.py"]
        mock_backend.read_content.assert_called_once_with("hash123")

    def test_returns_none_when_hash_not_found(self) -> None:
        """Hash not in CAS → returns None."""
        mock_backend = MagicMock()
        mock_backend.read_content.return_value = None

        reader = CASManifestReader(backend=mock_backend)
        paths = reader.read_file_paths("missing-hash")

        assert paths is None

    def test_returns_none_on_invalid_json(self) -> None:
        """Invalid JSON in CAS → returns None (logged warning)."""
        mock_backend = MagicMock()
        mock_backend.read_content.return_value = b"not valid json"

        reader = CASManifestReader(backend=mock_backend)
        paths = reader.read_file_paths("bad-hash")

        assert paths is None
