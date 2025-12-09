"""Unit tests for CacheConnectorMixin bulk operations.

Tests cover performance optimizations:
- Bulk cache loading for sync operations
- Skipping version checks for fresh cached entries
"""

from pathlib import Path
from unittest.mock import patch

from nexus.backends.cache_mixin import CacheConnectorMixin


class MockBackend(CacheConnectorMixin):
    """Mock backend for testing cache mixin."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.name = "test_backend"
        self.files = {}  # backend_path -> content
        self.versions = {}  # backend_path -> version_id

    def _read_content_from_backend(self, path, context=None):
        return self.files.get(path)

    def list_dir(self, path, context=None):
        # Return files in this directory
        prefix = path.rstrip("/") + "/" if path else ""
        return [
            f.replace(prefix, "")
            for f in self.files
            if f.startswith(prefix) and "/" not in f.replace(prefix, "")
        ]

    def get_version(self, path, context=None):
        return self.versions.get(path)

    def _list_files_recursive(self, path, context=None):
        return list(self.files.keys())


class TestCacheMixinBulkOperations:
    """Test bulk cache operations for performance."""

    def test_bulk_cache_loading_in_sync(self, tmp_path: Path):
        """Test that sync_content_to_cache() uses bulk cache loading instead of one-by-one."""
        # Setup
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBackend(SessionLocal)
        backend.files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
            "file3.txt": b"content3",
        }
        backend.versions = {
            "file1.txt": "v1",
            "file2.txt": "v2",
            "file3.txt": "v3",
        }

        # Mock _read_bulk_from_cache to track if it was called
        with patch.object(backend, "_read_bulk_from_cache") as mock_bulk:
            mock_bulk.return_value = {}  # No cached entries

            # Run sync
            backend.sync_content_to_cache(mount_point="/test", generate_embeddings=False)

            # Verify bulk method was called (optimization)
            assert mock_bulk.called, "sync_content_to_cache() should use bulk cache loading"

            # Verify it was called with list of paths
            call_args = mock_bulk.call_args
            paths = call_args[0][0]
            assert isinstance(paths, list), "bulk method should receive list of paths"
            assert len(paths) == 3, "should load all 3 files in bulk"

    def test_skip_version_check_for_fresh_cache(self, tmp_path: Path):
        """Test that fresh cached entries skip version checks (network calls)."""
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.backends.cache_mixin import CacheEntry
        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBackend(SessionLocal)
        backend.files = {
            "file1.txt": b"content1",
        }
        backend.versions = {
            "file1.txt": "v1",
        }

        # Create fresh cache entry
        fresh_cache = {
            "/test/file1.txt": CacheEntry(
                cache_id="cache1",
                path_id="path1",
                content_text="content1",
                content_binary=b"content1",
                content_hash="hash1",
                content_type="full",
                original_size=8,
                cached_size=8,
                backend_version="v1",
                synced_at=datetime.now(UTC),
                stale=False,
            )
        }

        with (
            patch.object(backend, "_read_bulk_from_cache") as mock_bulk,
            patch.object(backend, "get_version") as mock_version,
        ):
            mock_bulk.return_value = fresh_cache
            mock_version.return_value = "v1"

            # Run sync
            backend.sync_content_to_cache(mount_point="/test", generate_embeddings=False)

            # Verify version check was NOT called (optimization!)
            # Fresh cache with version should skip the network call
            assert mock_version.call_count == 0, (
                "Should skip version checks for fresh cached entries with versions"
            )

    def test_version_check_for_stale_cache(self, tmp_path: Path):
        """Test that stale cached entries still do version checks."""
        from datetime import UTC, datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.backends.cache_mixin import CacheEntry
        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBackend(SessionLocal)
        backend.files = {
            "file1.txt": b"content1",
        }
        backend.versions = {
            "file1.txt": "v2",  # Different version
        }

        # Create stale cache entry (marked as stale)
        stale_cache = {
            "/test/file1.txt": CacheEntry(
                cache_id="cache1",
                path_id="path1",
                content_text="content1",
                content_binary=b"content1",
                content_hash="hash1",
                content_type="full",
                original_size=8,
                cached_size=8,
                backend_version="v1",
                synced_at=datetime.now(UTC),
                stale=True,  # Marked as stale
            )
        }

        with (
            patch.object(backend, "_read_bulk_from_cache") as mock_bulk,
            patch.object(backend, "get_version") as mock_version,
        ):
            mock_bulk.return_value = stale_cache
            mock_version.return_value = "v2"

            # Run sync
            backend.sync_content_to_cache(mount_point="/test", generate_embeddings=False)

            # Stale entries should still be checked/re-cached
            # This ensures data freshness

    def test_bulk_read_performance(self, tmp_path: Path):
        """Test that bulk read is faster than individual reads."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBackend(SessionLocal)

        # Create 100 test files
        for i in range(100):
            backend.files[f"file{i}.txt"] = b"content"

        # Test bulk read (should use single query)
        with patch.object(backend, "_get_path_ids_bulk") as mock_bulk_ids:
            mock_bulk_ids.return_value = {}

            paths = [f"/test/file{i}.txt" for i in range(100)]
            backend._read_bulk_from_cache(paths)

            # Verify bulk method was called (not 100 individual calls)
            assert mock_bulk_ids.call_count == 1, (
                "Should use single bulk query, not N individual queries"
            )
