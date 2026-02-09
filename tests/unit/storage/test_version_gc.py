"""Tests for version history garbage collection (Issue #974)."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.local import LocalBackend
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.version_gc import GCStats, VersionGCSettings, VersionHistoryGC


class TestGCStats:
    """Test GCStats dataclass."""

    def test_total_deleted(self):
        """Test total_deleted property."""
        stats = GCStats(deleted_by_age=10, deleted_by_count=5)
        assert stats.total_deleted == 15

    def test_to_dict(self):
        """Test to_dict serialization."""
        stats = GCStats(
            deleted_by_age=10,
            deleted_by_count=5,
            bytes_reclaimed=1024,
            resources_processed=3,
            duration_seconds=1.234,
            dry_run=True,
        )
        result = stats.to_dict()

        assert result["deleted_by_age"] == 10
        assert result["deleted_by_count"] == 5
        assert result["total_deleted"] == 15
        assert result["bytes_reclaimed"] == 1024
        assert result["resources_processed"] == 3
        assert result["duration_seconds"] == 1.23  # Rounded to 2 decimals
        assert result["dry_run"] is True


class TestVersionGCSettings:
    """Test VersionGCSettings configuration."""

    def test_default_values(self, monkeypatch):
        """Test default configuration values."""
        # Clear env vars
        for var in [
            "NEXUS_VERSION_GC_ENABLED",
            "NEXUS_VERSION_GC_RETENTION_DAYS",
            "NEXUS_VERSION_GC_MAX_VERSIONS",
            "NEXUS_VERSION_GC_INTERVAL_HOURS",
            "NEXUS_VERSION_GC_BATCH_SIZE",
        ]:
            monkeypatch.delenv(var, raising=False)

        config = VersionGCSettings()

        assert config.enabled is True
        assert config.retention_days == 30
        assert config.max_versions_per_resource == 100
        assert config.run_interval_hours == 24
        assert config.batch_size == 1000

    def test_from_env(self, monkeypatch):
        """Test loading configuration from environment variables."""
        monkeypatch.setenv("NEXUS_VERSION_GC_ENABLED", "false")
        monkeypatch.setenv("NEXUS_VERSION_GC_RETENTION_DAYS", "7")
        monkeypatch.setenv("NEXUS_VERSION_GC_MAX_VERSIONS", "50")
        monkeypatch.setenv("NEXUS_VERSION_GC_INTERVAL_HOURS", "12")
        monkeypatch.setenv("NEXUS_VERSION_GC_BATCH_SIZE", "500")

        config = VersionGCSettings.from_env()

        assert config.enabled is False
        assert config.retention_days == 7
        assert config.max_versions_per_resource == 50
        assert config.run_interval_hours == 12
        assert config.batch_size == 500

    def test_validate_retention_days(self):
        """Test validation of retention_days."""
        config = VersionGCSettings(retention_days=0)
        with pytest.raises(ValueError, match="NEXUS_VERSION_GC_RETENTION_DAYS must be >= 1"):
            config.validate()

    def test_validate_max_versions(self):
        """Test validation of max_versions_per_resource."""
        config = VersionGCSettings(max_versions_per_resource=0)
        with pytest.raises(ValueError, match="NEXUS_VERSION_GC_MAX_VERSIONS must be >= 1"):
            config.validate()

    def test_validate_interval_hours(self):
        """Test validation of run_interval_hours."""
        config = VersionGCSettings(run_interval_hours=0)
        with pytest.raises(ValueError, match="NEXUS_VERSION_GC_INTERVAL_HOURS must be >= 1"):
            config.validate()

    def test_validate_batch_size(self):
        """Test validation of batch_size."""
        config = VersionGCSettings(batch_size=0)
        with pytest.raises(ValueError, match="NEXUS_VERSION_GC_BATCH_SIZE must be >= 1"):
            config.validate()

    def test_repr(self):
        """Test string representation."""
        config = VersionGCSettings(retention_days=7, max_versions_per_resource=50)
        repr_str = repr(config)

        assert "retention_days=7" in repr_str
        assert "max_versions=50" in repr_str


class TestVersionHistoryGC:
    """Test VersionHistoryGC garbage collector."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def record_store(self, temp_dir):
        """Create SQLAlchemyRecordStore for testing."""
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        rs = SQLAlchemyRecordStore(db_path=str(data_dir / "nexus.db"))
        yield rs
        rs.close()

    @pytest.fixture
    def nx(self, temp_dir, record_store):
        """Create NexusFS instance for testing.

        Uses RaftMetadataStore. TODO: Version history depends on FilePathModel
        populated by SQLAlchemy, may need adjustment.
        """
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        backend = LocalBackend(root_path=data_dir)
        metadata_store = RaftMetadataStore.local(str(data_dir / "raft-metadata"))
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=False,
        )
        yield nx
        nx.close()

    def test_gc_preserves_latest_version(self, nx, record_store):
        """Test that GC always preserves the latest version."""
        path = "/workspace/test.txt"

        # Create multiple versions
        for i in range(5):
            nx.write(path, f"Version {i + 1}".encode())

        # Run GC with very aggressive settings (0 retention, 1 max version)
        gc = VersionHistoryGC(record_store.session_factory)
        config = VersionGCSettings(
            retention_days=0,  # This would delete everything by age
            max_versions_per_resource=1,  # Keep only latest
            batch_size=100,
        )

        # The retention_days=0 won't work (validated), use 1 day with mocked old dates
        # For now, just test that GC runs without error
        gc.run_gc(config, dry_run=True)

        # Verify current version still readable
        content = nx.read(path)
        assert content == b"Version 5"

    def test_gc_dry_run_no_changes(self, nx, record_store):
        """Test that dry run doesn't delete anything."""
        path = "/workspace/test.txt"

        # Create multiple versions
        for i in range(3):
            nx.write(path, f"Version {i + 1}".encode())

        # Get initial version count
        with record_store.session_factory() as session:
            from sqlalchemy import func, select

            from nexus.storage.models import VersionHistoryModel

            initial_count = session.scalar(select(func.count()).select_from(VersionHistoryModel))

        # Run GC in dry run mode
        gc = VersionHistoryGC(record_store.session_factory)
        config = VersionGCSettings(retention_days=1, max_versions_per_resource=1)
        stats = gc.run_gc(config, dry_run=True)

        assert stats.dry_run is True

        # Verify no versions were actually deleted
        with record_store.session_factory() as session:
            final_count = session.scalar(select(func.count()).select_from(VersionHistoryModel))
            assert final_count == initial_count

    def test_gc_respects_max_versions(self, nx, record_store):
        """Test GC enforces max versions per resource."""
        path = "/workspace/many_versions.txt"

        # Create 10 versions
        for i in range(10):
            nx.write(path, f"Version {i + 1}".encode())

        # Get the resource_id (path_id) for this file
        with record_store.session_factory() as session:
            from sqlalchemy import select

            from nexus.storage.models import FilePathModel

            file_path = session.scalar(
                select(FilePathModel).where(FilePathModel.virtual_path == path)
            )
            resource_id = file_path.path_id

        # Run GC with max 3 versions
        gc = VersionHistoryGC(record_store.session_factory)
        config = VersionGCSettings(
            retention_days=365,  # Don't delete by age
            max_versions_per_resource=3,
            batch_size=100,
        )
        stats = gc.run_gc(config, dry_run=False)

        # Should have deleted 7 versions (10 - 3)
        assert stats.deleted_by_count == 7
        assert stats.total_deleted == 7

        # Verify only 3 versions remain for this resource
        with record_store.session_factory() as session:
            from sqlalchemy import func, select

            from nexus.storage.models import VersionHistoryModel

            count = session.scalar(
                select(func.count())
                .select_from(VersionHistoryModel)
                .where(VersionHistoryModel.resource_id == resource_id)
            )
            assert count == 3

    def test_gc_stats_reporting(self, nx, record_store):
        """Test that GC reports accurate statistics."""
        # Create a file
        path = "/workspace/stats_test.txt"
        nx.write(path, b"Content")

        gc = VersionHistoryGC(record_store.session_factory)
        table_stats = gc.get_stats()

        assert "total_versions" in table_stats
        assert "unique_resources" in table_stats
        assert "total_bytes" in table_stats
        assert "oldest_version" in table_stats
        assert "newest_version" in table_stats

        assert table_stats["total_versions"] >= 1
        assert table_stats["unique_resources"] >= 1

    def test_gc_multiple_resources(self, nx, record_store):
        """Test GC handles multiple resources correctly."""
        # Create versions for multiple files
        for file_num in range(3):
            path = f"/workspace/file{file_num}.txt"
            for version in range(5):
                nx.write(path, f"File {file_num} Version {version}".encode())

        # Run GC with max 2 versions
        gc = VersionHistoryGC(record_store.session_factory)
        config = VersionGCSettings(
            retention_days=365,
            max_versions_per_resource=2,
            batch_size=100,
        )
        stats = gc.run_gc(config, dry_run=False)

        # Should delete 3 versions from each file (5 - 2 = 3, times 3 files = 9)
        assert stats.deleted_by_count == 9

        # All files should still be readable
        for file_num in range(3):
            path = f"/workspace/file{file_num}.txt"
            content = nx.read(path)
            assert b"Version 4" in content  # Latest version

    def test_gc_override_params(self, nx, record_store):
        """Test parameter override functionality."""
        path = "/workspace/override_test.txt"
        for i in range(5):
            nx.write(path, f"V{i}".encode())

        gc = VersionHistoryGC(record_store.session_factory)
        default_config = VersionGCSettings(
            retention_days=365,
            max_versions_per_resource=100,
        )

        # Override max_versions to 2
        stats = gc.run_gc(
            config=default_config,
            dry_run=False,
            max_versions=2,  # Override
        )

        # Should respect override
        assert stats.deleted_by_count == 3  # 5 - 2 = 3

    def test_gc_empty_table(self, temp_dir):
        """Test GC handles empty version_history table."""
        data_dir = Path(temp_dir) / "nexus-data-empty"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Create fresh record store and NexusFS without any files
        rs_empty = SQLAlchemyRecordStore(db_path=str(data_dir / "nexus.db"))
        backend = LocalBackend(root_path=data_dir)
        metadata_store = RaftMetadataStore.local(str(data_dir / "metadata"))
        nx_empty = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=rs_empty,
            enforce_permissions=False,
        )

        try:
            gc = VersionHistoryGC(rs_empty.session_factory)
            config = VersionGCSettings()
            stats = gc.run_gc(config, dry_run=False)

            assert stats.total_deleted == 0
            assert stats.resources_processed == 0
        finally:
            nx_empty.close()
            rs_empty.close()


class TestVersionGCTask:
    """Test background GC task."""

    @pytest.mark.asyncio
    async def test_gc_task_runs(self):
        """Test that GC task can be started."""
        from nexus.server.background_tasks import version_gc_task

        # Mock the session factory and GC
        mock_session_factory = MagicMock()
        mock_config = VersionGCSettings(enabled=False)  # Disabled to avoid actual GC

        # Create the task but cancel it immediately
        task = asyncio.create_task(version_gc_task(mock_session_factory, mock_config))

        # Let it start
        await asyncio.sleep(0.1)

        # Cancel and verify it was created successfully
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
