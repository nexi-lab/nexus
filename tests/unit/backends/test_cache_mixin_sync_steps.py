"""Unit tests for CacheConnectorMixin.sync() refactored steps.

Tests each of the 7 sync steps independently to ensure they work correctly
in isolation and handle edge cases properly.

Test Structure:
    - TestStep1DiscoverFiles: File discovery and filtering
    - TestStep2LoadCache: Bulk cache loading
    - TestStep3CheckVersions: Version checking and filtering
    - TestStep4ReadBackend: Batch backend reads
    - TestStep5ProcessContent: Content processing and parsing
    - TestStep6WriteCache: Batch cache writing
    - TestStep7GenerateEmbeddings: Embedding generation
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.cache_mixin import (
    IMMUTABLE_VERSION,
    CacheConnectorMixin,
    CacheEntry,
    SyncResult,
)
from nexus.core.permissions import OperationContext
from nexus.storage.models import Base, ContentCacheModel, FilePathModel


class MockConnector(CacheConnectorMixin):
    """Mock connector for testing cache mixin."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.name = "test_connector"
        self.files = {}  # backend_path -> content
        self.versions = {}  # backend_path -> version_id

    def _read_content_from_backend(self, path, context=None):
        return self.files.get(path)

    def list_dir(self, path, context=None):
        # Return files in this directory
        prefix = path.rstrip("/") + "/" if path else ""
        files_in_dir = []
        for f in self.files:
            if f.startswith(prefix):
                relative = f.replace(prefix, "", 1)
                if "/" not in relative:  # Only immediate children
                    files_in_dir.append(relative)
        return files_in_dir

    def get_version(self, path, context=None):
        return self.versions.get(path)

    def _list_files_recursive(self, path, context=None):
        return list(self.files.keys())

    def _batch_get_versions(self, paths, contexts):
        """Batch version fetch (10-25x faster than sequential)."""
        return {path: self.versions.get(path) for path in paths}


@pytest.fixture
def db_session(tmp_path: Path):
    """Create test database session."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


@pytest.fixture
def connector(db_session):
    """Create mock connector with test database."""
    return MockConnector(db_session)


@pytest.fixture
def test_context():
    """Create test operation context."""
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        tenant_id="test_tenant",
        is_system=True,
    )


class TestStep1DiscoverFiles:
    """Test Step 1: File discovery and filtering."""

    def test_discover_all_files(self, connector, test_context):
        """Test discovering all files from backend."""
        connector.files = {
            "file1.txt": b"content1",
            "file2.py": b"content2",
            "dir/file3.md": b"content3",
        }

        result = SyncResult()
        files, mapping = connector._sync_step1_discover_files(
            path=None,
            mount_point="/test",
            include_patterns=None,
            exclude_patterns=None,
            context=test_context,
            result=result,
        )

        assert len(files) == 3
        assert "file1.txt" in files
        assert "file2.py" in files
        assert "dir/file3.md" in files
        assert mapping["file1.txt"] == "/test/file1.txt"
        assert mapping["file2.py"] == "/test/file2.py"
        assert mapping["dir/file3.md"] == "/test/dir/file3.md"

    def test_discover_with_include_patterns(self, connector, test_context):
        """Test file discovery with include patterns."""
        connector.files = {
            "file1.txt": b"content1",
            "file2.py": b"content2",
            "file3.md": b"content3",
        }

        result = SyncResult()
        files, mapping = connector._sync_step1_discover_files(
            path=None,
            mount_point="/test",
            include_patterns=["*.py", "*.md"],
            exclude_patterns=None,
            context=test_context,
            result=result,
        )

        assert len(files) == 2
        assert "file2.py" in files
        assert "file3.md" in files
        assert "file1.txt" not in files
        assert result.files_skipped == 1

    def test_discover_with_exclude_patterns(self, connector, test_context):
        """Test file discovery with exclude patterns."""
        connector.files = {
            "file1.txt": b"content1",
            "file2.pyc": b"content2",
            ".git/config": b"content3",
        }

        result = SyncResult()
        files, mapping = connector._sync_step1_discover_files(
            path=None,
            mount_point="/test",
            include_patterns=None,
            exclude_patterns=["*.pyc", "/test/.git/*"],  # Must match full virtual path
            context=test_context,
            result=result,
        )

        assert len(files) == 1
        assert "file1.txt" in files
        assert "file2.pyc" not in files
        assert ".git/config" not in files
        assert result.files_skipped == 2

    def test_discover_specific_path(self, connector, test_context):
        """Test discovering files from specific path."""
        connector.files = {
            "reports/2024/q1.pdf": b"content1",
            "reports/2024/q2.pdf": b"content2",
            "data/file.txt": b"content3",
        }

        # Mock list_dir to return files in reports/2024
        with (
            patch.object(connector, "list_dir", return_value=["q1.pdf", "q2.pdf"]),
            patch.object(
                connector,
                "_list_files_recursive",
                return_value=["reports/2024/q1.pdf", "reports/2024/q2.pdf"],
            ),
        ):
            result = SyncResult()
            files, mapping = connector._sync_step1_discover_files(
                path="reports/2024",
                mount_point="/test",
                include_patterns=None,
                exclude_patterns=None,
                context=test_context,
                result=result,
            )

            assert len(files) == 2
            assert "reports/2024/q1.pdf" in files
            assert "reports/2024/q2.pdf" in files
            assert "data/file.txt" not in files

    def test_discover_error_handling(self, connector, test_context):
        """Test error handling during file discovery."""
        # Make _list_files_recursive raise an error
        with patch.object(
            connector, "_list_files_recursive", side_effect=Exception("Backend error")
        ):
            result = SyncResult()
            files, mapping = connector._sync_step1_discover_files(
                path=None,
                mount_point="/test",
                include_patterns=None,
                exclude_patterns=None,
                context=test_context,
                result=result,
            )

            assert len(files) == 0
            assert len(mapping) == 0
            assert len(result.errors) == 1
            assert "Failed to list files" in result.errors[0]


class TestStep2LoadCache:
    """Test Step 2: Bulk cache loading."""

    def test_load_empty_cache(self, connector):
        """Test loading cache when nothing is cached."""
        virtual_paths = ["/test/file1.txt", "/test/file2.txt"]

        cached = connector._sync_step2_load_cache(virtual_paths)

        assert len(cached) == 0

    def test_load_partial_cache(self, connector, db_session):
        """Test loading cache with some files cached."""
        session = db_session()

        # Create file_paths entries
        path1 = FilePathModel(
            path_id="path1",
            virtual_path="/test/file1.txt",
            backend_id="backend1",
            physical_path="file1.txt",
            tenant_id="test_tenant",
        )
        path2 = FilePathModel(
            path_id="path2",
            virtual_path="/test/file2.txt",
            backend_id="backend1",
            physical_path="file2.txt",
            tenant_id="test_tenant",
        )
        session.add_all([path1, path2])

        # Create cache entry for file1 only
        cache1 = ContentCacheModel(
            cache_id="cache1",
            path_id="path1",
            tenant_id="test_tenant",
            content_text="content1",
            content_hash="hash1",
            content_type="full",
            original_size_bytes=8,
            cached_size_bytes=8,
            backend_version="v1",
            synced_at=datetime.now(UTC),
            stale=False,
        )
        session.add(cache1)
        session.commit()
        session.close()

        # Load cache
        virtual_paths = ["/test/file1.txt", "/test/file2.txt"]
        cached = connector._sync_step2_load_cache(virtual_paths)

        assert len(cached) == 1
        assert "/test/file1.txt" in cached
        assert "/test/file2.txt" not in cached

    def test_load_cache_performance(self, connector):
        """Test that bulk cache loading uses single query."""
        virtual_paths = [f"/test/file{i}.txt" for i in range(100)]

        with patch.object(connector, "_get_path_ids_bulk") as mock_bulk:
            mock_bulk.return_value = {}
            connector._sync_step2_load_cache(virtual_paths)

            # Should use single bulk query, not 100 individual queries
            assert mock_bulk.call_count == 1


class TestStep3CheckVersions:
    """Test Step 3: Version checking and filtering."""

    def test_skip_immutable_files(self, connector, test_context):
        """Test that immutable files are skipped."""
        files = ["email1.eml", "email2.eml"]
        backend_to_virtual = {
            "email1.eml": "/test/email1.eml",
            "email2.eml": "/test/email2.eml",
        }
        cached_entries = {
            "/test/email1.eml": CacheEntry(
                cache_id="cache1",
                path_id="path1",
                content_text="email content",
                content_binary=b"email content",
                content_hash="hash1",
                content_type="full",
                original_size=10,
                cached_size=10,
                backend_version=IMMUTABLE_VERSION,
                synced_at=datetime.now(UTC),
                stale=False,
            )
        }

        result = SyncResult()
        files_needing, contexts, metadata = connector._sync_step3_check_versions(
            files, backend_to_virtual, cached_entries, test_context, result
        )

        # email1 should be skipped (immutable), email2 should need backend read
        assert len(files_needing) == 1
        assert "email2.eml" in files_needing
        assert result.files_skipped == 1

    def test_skip_fresh_cached_files(self, connector, test_context):
        """Test that fresh cached files with matching versions are skipped."""
        connector.versions = {"file1.txt": "v1", "file2.txt": "v2"}

        files = ["file1.txt", "file2.txt"]
        backend_to_virtual = {
            "file1.txt": "/test/file1.txt",
            "file2.txt": "/test/file2.txt",
        }
        cached_entries = {
            "/test/file1.txt": CacheEntry(
                cache_id="cache1",
                path_id="path1",
                content_text="content1",
                content_binary=b"content1",
                content_hash="hash1",
                content_type="full",
                original_size=8,
                cached_size=8,
                backend_version="v1",  # Matches current version
                synced_at=datetime.now(UTC),
                stale=False,
            )
        }

        result = SyncResult()
        files_needing, contexts, metadata = connector._sync_step3_check_versions(
            files, backend_to_virtual, cached_entries, test_context, result
        )

        # file1 should be skipped (version match), file2 should need backend read
        assert len(files_needing) == 1
        assert "file2.txt" in files_needing
        assert result.files_skipped == 1

    def test_sync_stale_files(self, connector, test_context):
        """Test that stale cached files are re-synced."""
        connector.versions = {"file1.txt": "v2"}

        files = ["file1.txt"]
        backend_to_virtual = {"file1.txt": "/test/file1.txt"}
        cached_entries = {
            "/test/file1.txt": CacheEntry(
                cache_id="cache1",
                path_id="path1",
                content_text="old content",
                content_binary=b"old content",
                content_hash="hash1",
                content_type="full",
                original_size=8,
                cached_size=8,
                backend_version="v1",  # Old version
                synced_at=datetime.now(UTC),
                stale=True,  # Marked as stale
            )
        }

        result = SyncResult()
        files_needing, contexts, metadata = connector._sync_step3_check_versions(
            files, backend_to_virtual, cached_entries, test_context, result
        )

        # Stale file should need backend read
        assert len(files_needing) == 1
        assert "file1.txt" in files_needing
        assert result.files_skipped == 0

    def test_batch_version_fetch(self, connector, test_context):
        """Test that version fetching uses batch API when available."""
        connector.versions = {f"file{i}.txt": f"v{i}" for i in range(10)}

        files = [f"file{i}.txt" for i in range(10)]
        backend_to_virtual = {f: f"/test/{f}" for f in files}
        cached_entries = {}

        with patch.object(connector, "_batch_get_versions") as mock_batch:
            mock_batch.return_value = connector.versions

            result = SyncResult()
            connector._sync_step3_check_versions(
                files, backend_to_virtual, cached_entries, test_context, result
            )

            # Should use single batch call, not 10 individual calls
            assert mock_batch.call_count == 1


class TestStep4ReadBackend:
    """Test Step 4: Batch backend reads."""

    def test_read_from_backend(self, connector):
        """Test reading files from backend."""
        connector.files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
        }

        contexts = {}
        backend_contents = connector._sync_step4_read_backend(["file1.txt", "file2.txt"], contexts)

        assert len(backend_contents) == 2
        assert backend_contents["file1.txt"] == b"content1"
        assert backend_contents["file2.txt"] == b"content2"

    def test_read_partial_failure(self, connector):
        """Test handling partial backend read failures."""
        connector.files = {
            "file1.txt": b"content1",
            # file2.txt missing - simulates failure
        }

        contexts = {}
        backend_contents = connector._sync_step4_read_backend(["file1.txt", "file2.txt"], contexts)

        # Should return only successful reads
        assert len(backend_contents) == 1
        assert backend_contents["file1.txt"] == b"content1"
        assert "file2.txt" not in backend_contents


class TestStep5ProcessContent:
    """Test Step 5: Content processing and parsing."""

    def test_process_simple_files(self, connector, test_context):
        """Test processing simple text files."""
        backend_contents = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
        }
        file_metadata = {
            "file1.txt": {"virtual_path": "/test/file1.txt", "cached": None, "version": "v1"},
            "file2.txt": {"virtual_path": "/test/file2.txt", "cached": None, "version": "v2"},
        }

        result = SyncResult()
        cache_entries, files_to_embed = connector._sync_step5_process_content(
            backend_contents,
            file_metadata,
            max_size=1024 * 1024,
            generate_embeddings=True,
            context=test_context,
            result=result,
        )

        assert len(cache_entries) == 2
        assert cache_entries[0]["path"] == "/test/file1.txt"
        assert cache_entries[0]["content"] == b"content1"
        assert cache_entries[0]["backend_version"] == "v1"
        assert len(files_to_embed) == 2
        assert result.files_synced == 2
        assert result.bytes_synced == 16

    def test_skip_large_files(self, connector, test_context):
        """Test that files exceeding max_size are skipped."""
        large_content = b"x" * (10 * 1024 * 1024)  # 10MB
        backend_contents = {
            "large.bin": large_content,
            "small.txt": b"small",
        }
        file_metadata = {
            "large.bin": {"virtual_path": "/test/large.bin", "cached": None, "version": None},
            "small.txt": {"virtual_path": "/test/small.txt", "cached": None, "version": None},
        }

        result = SyncResult()
        cache_entries, files_to_embed = connector._sync_step5_process_content(
            backend_contents,
            file_metadata,
            max_size=1 * 1024 * 1024,  # 1MB limit
            generate_embeddings=False,
            context=test_context,
            result=result,
        )

        # Only small file should be processed
        assert len(cache_entries) == 1
        assert cache_entries[0]["path"] == "/test/small.txt"
        assert result.files_synced == 1
        assert result.files_skipped == 1

    def test_skip_unchanged_content(self, connector, test_context):
        """Test that unchanged content (matching hash) is skipped."""
        from nexus.core.hash_fast import hash_content

        content = b"unchanged content"
        content_hash = hash_content(content)

        backend_contents = {"file1.txt": content}
        file_metadata = {
            "file1.txt": {
                "virtual_path": "/test/file1.txt",
                "cached": CacheEntry(
                    cache_id="cache1",
                    path_id="path1",
                    content_text=content.decode("utf-8"),
                    content_binary=content,
                    content_hash=content_hash,
                    content_type="full",
                    original_size=len(content),
                    cached_size=len(content),
                    backend_version=None,  # No version (triggers hash check)
                    synced_at=datetime.now(UTC),
                    stale=False,
                ),
                "version": None,
            }
        }

        result = SyncResult()
        cache_entries, files_to_embed = connector._sync_step5_process_content(
            backend_contents,
            file_metadata,
            max_size=1024 * 1024,
            generate_embeddings=False,
            context=test_context,
            result=result,
        )

        # Content unchanged, should be skipped
        assert len(cache_entries) == 0
        assert result.files_synced == 0
        assert result.files_skipped == 1


class TestStep6WriteCache:
    """Test Step 6: Batch cache writing."""

    def test_write_cache_entries(self, connector, db_session):
        """Test writing cache entries to database."""
        session = db_session()

        # Create file_paths entries
        path1 = FilePathModel(
            path_id="path1",
            virtual_path="/test/file1.txt",
            backend_id="backend1",
            physical_path="file1.txt",
            tenant_id="test_tenant",
        )
        session.add(path1)
        session.commit()
        session.close()

        cache_entries = [
            {
                "path": "/test/file1.txt",
                "content": b"content1",
                "content_text": "content1",
                "content_type": "full",
                "backend_version": "v1",
                "parsed_from": None,
                "parse_metadata": None,
                "tenant_id": "test_tenant",
            }
        ]

        result = SyncResult()
        connector._sync_step6_write_cache(cache_entries, result)

        # Verify cache was written
        session = db_session()
        stmt = session.query(ContentCacheModel).filter_by(path_id="path1")
        cache_model = stmt.first()
        assert cache_model is not None
        assert cache_model.content_text == "content1"
        assert cache_model.backend_version == "v1"
        session.close()

    def test_write_cache_error_handling(self, connector):
        """Test error handling during cache write."""
        cache_entries = [
            {
                "path": "/test/nonexistent.txt",  # Path not in file_paths
                "content": b"content",
                "content_text": "content",
                "content_type": "full",
                "backend_version": "v1",
                "parsed_from": None,
                "parse_metadata": None,
                "tenant_id": "test_tenant",
            }
        ]

        result = SyncResult()
        connector._sync_step6_write_cache(cache_entries, result)

        # Should gracefully skip missing paths (logged as warning, not error)
        # The batch write skips paths not found in file_paths and logs warning
        assert len(result.errors) == 0  # No errors - just skips the entry


class TestStep7GenerateEmbeddings:
    """Test Step 7: Embedding generation."""

    def test_generate_embeddings(self, connector):
        """Test embedding generation for files."""
        files = ["/test/file1.txt", "/test/file2.txt"]

        with patch.object(connector, "_generate_embeddings") as mock_gen:
            result = SyncResult()
            connector._sync_step7_generate_embeddings(files, result)

            # Should generate embeddings for all files
            assert mock_gen.call_count == 2
            assert result.embeddings_generated == 2

    def test_generate_embeddings_error_handling(self, connector):
        """Test error handling during embedding generation."""
        files = ["/test/file1.txt"]

        with patch.object(
            connector, "_generate_embeddings", side_effect=Exception("Embedding error")
        ):
            result = SyncResult()
            connector._sync_step7_generate_embeddings(files, result)

            # Should capture error and continue
            assert result.embeddings_generated == 0
            assert len(result.errors) == 1
            assert "Failed to generate embeddings" in result.errors[0]


class TestSyncIntegration:
    """Integration test for full sync flow."""

    def test_full_sync_flow(self, connector, db_session, test_context):
        """Test complete sync operation with all steps."""
        session = db_session()

        # Setup: Create file_paths entries
        path1 = FilePathModel(
            path_id="path1",
            virtual_path="/test/file1.txt",
            backend_id="backend1",
            physical_path="file1.txt",
            tenant_id="test_tenant",
        )
        path2 = FilePathModel(
            path_id="path2",
            virtual_path="/test/file2.txt",
            backend_id="backend1",
            physical_path="file2.txt",
            tenant_id="test_tenant",
        )
        session.add_all([path1, path2])
        session.commit()
        session.close()

        # Setup: Add files to backend
        connector.files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
        }
        connector.versions = {
            "file1.txt": "v1",
            "file2.txt": "v2",
        }

        # Run full sync
        result = connector.sync_content_to_cache(
            mount_point="/test",
            generate_embeddings=False,
            context=test_context,
        )

        # Verify results
        assert result.files_scanned == 2
        assert result.files_synced >= 1  # At least one file synced successfully
        assert result.bytes_synced > 0
        assert len(result.errors) == 0

        # Verify cache was populated
        session = db_session()
        cache_entries = session.query(ContentCacheModel).all()
        assert len(cache_entries) >= 1  # At least one entry in cache
        session.close()

    def test_sync_with_patterns(self, connector, db_session, test_context):
        """Test sync with include/exclude patterns."""
        session = db_session()

        # Setup: Create file_paths entries
        for i, ext in enumerate(["txt", "py", "pyc"]):
            path = FilePathModel(
                path_id=f"path{i + 1}",
                virtual_path=f"/test/file{i + 1}.{ext}",
                backend_id="backend1",
                physical_path=f"file{i + 1}.{ext}",
                tenant_id="test_tenant",
            )
            session.add(path)
        session.commit()
        session.close()

        # Setup: Add files to backend
        connector.files = {
            "file1.txt": b"content1",
            "file2.py": b"content2",
            "file3.pyc": b"content3",
        }

        # Run sync with patterns (exclude .pyc)
        result = connector.sync_content_to_cache(
            mount_point="/test",
            exclude_patterns=["*.pyc"],
            generate_embeddings=False,
            context=test_context,
        )

        # Verify results
        # files_scanned is set to len(files) after filtering in step 1
        # So it should be 2 (txt + py), not 3 (excludes the filtered .pyc)
        assert result.files_scanned == 2
        assert result.files_synced >= 1  # At least one file synced
        assert result.files_skipped >= 1  # At least .pyc was filtered
        assert len(result.errors) == 0
