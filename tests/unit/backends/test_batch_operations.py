"""Unit tests for batch operations in connector backends.

Tests cover batch optimization methods:
- _batch_get_versions() for GCS and S3
- _bulk_download() for parallel downloads
- _batch_write_to_cache() for bulk cache writes
- _batch_read_from_backend() integration
- batch_read_content() native implementations for GCS and S3 (#1626)
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

from nexus.backends.base.backend import Backend
from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.contracts.types import OperationContext
from nexus.core.object_store import WriteResult


class InMemoryTransport:
    """Minimal in-memory Transport for tests."""

    transport_name = "memory"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.download_count: int = 0

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        self.files[key] = data
        return None

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        self.download_count += 1
        if key not in self.files:
            raise FileNotFoundError(f"Blob not found: {key}")
        return self.files[key], version_id

    def remove(self, key: str) -> None:
        self.files.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.files

    def get_size(self, key: str) -> int:
        return len(self.files.get(key, b""))

    def list_keys(self, prefix: str = "", delimiter: str = "/") -> tuple[list[str], list[str]]:
        keys = [k for k in self.files if k.startswith(prefix)]
        return keys, []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        if src_key in self.files:
            self.files[dst_key] = self.files[src_key]

    def create_dir(self, key: str) -> None:
        self.files[key] = b""

    def stream(
        self, key: str, chunk_size: int = 8192, version_id: str | None = None
    ) -> Iterator[bytes]:
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


class MockBlobConnector(PathAddressingEngine):
    """Mock blob connector for testing batch operations."""

    def __init__(self, session_factory):
        transport = InMemoryTransport()
        super().__init__(transport, backend_name="test_blob_backend")
        self.session_factory = session_factory
        self.versions: dict[str, str] = {}

    @property
    def files(self) -> dict[str, bytes]:
        """Proxy to transport.files for test manipulation."""
        return self._transport.files

    @files.setter
    def files(self, value: dict[str, bytes]) -> None:
        self._transport.files = value

    @property
    def download_count(self) -> int:
        """Proxy to transport.download_count for test assertions."""
        return self._transport.download_count

    @download_count.setter
    def download_count(self, value: int) -> None:
        self._transport.download_count = value

    def get_version(self, path: str, context: OperationContext | None = None) -> str | None:
        """Get version for a file."""
        return self.versions.get(path)


class TestBatchGetVersions:
    """Test batch_get_versions() method."""

    def test_batch_get_versions_default_fallback(self, tmp_path: Path):
        """Test default fallback implementation calls get_version() sequentially."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.versions = {
            "file1.txt": "v1",
            "file2.txt": "v2",
            "file3.txt": "v3",
        }

        # Call batch method
        result = backend.batch_get_versions(["file1.txt", "file2.txt", "file3.txt"])

        # Verify all versions returned
        assert result == {"file1.txt": "v1", "file2.txt": "v2", "file3.txt": "v3"}

    def test_batch_get_versions_handles_missing_files(self, tmp_path: Path):
        """Test batch get versions gracefully handles missing files."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.versions = {
            "file1.txt": "v1",
            # file2.txt doesn't exist
            "file3.txt": "v3",
        }

        # Call batch method
        result = backend.batch_get_versions(["file1.txt", "file2.txt", "file3.txt"])

        # Should return None for missing files
        assert result == {"file1.txt": "v1", "file2.txt": None, "file3.txt": "v3"}

    def test_batch_get_versions_empty_list(self, tmp_path: Path):
        """Test batch get versions with empty list."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Call with empty list
        result = backend.batch_get_versions([])

        # Should return empty dict
        assert result == {}


class TestBulkDownloadBlobs:
    """Test _bulk_download() method."""

    def test_bulk_download_calls_download(self, tmp_path: Path):
        """Test that bulk download calls _download() for each file (DRY principle)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.files = {
            "blob1.txt": b"content1",
            "blob2.txt": b"content2",
            "blob3.txt": b"content3",
        }

        # Reset download count
        backend.download_count = 0

        # Call bulk download
        result = backend._bulk_download(["blob1.txt", "blob2.txt", "blob3.txt"], max_workers=2)

        # Verify _download() was called for each file
        assert backend.download_count == 3, "_bulk_download should call _download() for each file"

        # Verify all files downloaded
        assert len(result) == 3
        assert result["blob1.txt"] == b"content1"
        assert result["blob2.txt"] == b"content2"
        assert result["blob3.txt"] == b"content3"

    def test_bulk_download_handles_failures_gracefully(self, tmp_path: Path):
        """Test bulk download continues even if some files fail."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.files = {
            "blob1.txt": b"content1",
            # blob2.txt is missing (will fail)
            "blob3.txt": b"content3",
        }

        # Call bulk download
        result = backend._bulk_download(["blob1.txt", "blob2.txt", "blob3.txt"], max_workers=2)

        # Should return successful downloads only
        assert len(result) == 2
        assert result["blob1.txt"] == b"content1"
        assert result["blob3.txt"] == b"content3"
        assert "blob2.txt" not in result

    def test_bulk_download_empty_list(self, tmp_path: Path):
        """Test bulk download with empty list."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Call with empty list
        result = backend._bulk_download([], max_workers=2)

        # Should return empty dict
        assert result == {}


# === GCS batch_read_content tests (#1626) ===


class MockGCSBackend(Backend):
    """Mock GCS backend for testing batch_read_content.

    Simulates CAS-based hash-to-blob path mapping without real GCS dependencies.
    """

    batch_read_workers: int = 4  # Low for tests

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}  # hash -> content
        self.read_count: int = 0

    @property
    def name(self) -> str:
        return "gcs"

    def write_content(
        self, content, content_id: str = "", *, offset: int = 0, context=None
    ) -> WriteResult:
        from nexus.core.hash_fast import hash_content

        h = hash_content(content)
        self._content[h] = content
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, content_id, context=None) -> bytes:
        self.read_count += 1
        if content_id not in self._content:
            raise NexusFileNotFoundError(content_id)
        return self._content[content_id]

    def batch_read_content(
        self, content_ids, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        """Use the same parallel logic as GCSBackend."""
        if not content_ids:
            return {}

        result: dict[str, bytes | None] = {}

        if len(content_ids) == 1:
            try:
                data = self.read_content(content_ids[0], context=context)
                return {content_ids[0]: data}
            except (NexusFileNotFoundError, BackendError):
                return {content_ids[0]: None}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(self.batch_read_workers, len(content_ids))

        def read_one(content_id: str) -> tuple[str, bytes | None]:
            try:
                data = self.read_content(content_id, context=context)
                return (content_id, data)
            except (NexusFileNotFoundError, BackendError):
                return (content_id, None)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(read_one, h): h for h in content_ids}
            for future in as_completed(futures):
                hash_key, file_content = future.result()
                result[hash_key] = file_content

        return result

    def delete_content(self, content_id, context=None) -> None:
        self._content.pop(content_id, None)

    def content_exists(self, content_id, context=None) -> bool:
        return content_id in self._content

    def get_content_size(self, content_id, context=None) -> int:
        if content_id not in self._content:
            raise NexusFileNotFoundError(content_id)
        return len(self._content[content_id])

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> None:
        pass

    def rmdir(self, path, recursive=False, context=None) -> None:
        pass

    def is_directory(self, path, context=None) -> bool:
        return False


class TestGCSBatchReadContent:
    """Test GCS-style CAS batch_read_content with parallel ThreadPoolExecutor."""

    def test_basic_batch_read(self):
        """Test reading multiple CAS objects in parallel."""
        backend = MockGCSBackend()
        h1 = backend.write_content(b"file1").content_id
        h2 = backend.write_content(b"file2").content_id
        h3 = backend.write_content(b"file3").content_id

        result = backend.batch_read_content([h1, h2, h3])

        assert result[h1] == b"file1"
        assert result[h2] == b"file2"
        assert result[h3] == b"file3"

    def test_partial_failures_return_none(self):
        """Test missing hashes return None, not exceptions."""
        backend = MockGCSBackend()
        h1 = backend.write_content(b"exists").content_id
        fake_hash = "a" * 64

        result = backend.batch_read_content([h1, fake_hash])

        assert result[h1] == b"exists"
        assert result[fake_hash] is None

    def test_empty_list_returns_empty_dict(self):
        """Test empty input returns empty dict."""
        backend = MockGCSBackend()
        assert backend.batch_read_content([]) == {}

    def test_single_item_skips_thread_pool(self):
        """Test single-item optimization (no ThreadPoolExecutor overhead)."""
        backend = MockGCSBackend()
        h1 = backend.write_content(b"single").content_id

        result = backend.batch_read_content([h1])

        assert result[h1] == b"single"
        assert backend.read_count == 1

    def test_parallel_execution(self):
        """Test that reads happen in parallel (all items read)."""
        backend = MockGCSBackend()
        hashes = []
        for i in range(20):
            h = backend.write_content(f"content{i}".encode()).content_id
            hashes.append(h)

        backend.read_count = 0
        result = backend.batch_read_content(hashes)

        assert len(result) == 20
        assert backend.read_count == 20
        assert all(v is not None for v in result.values())

    def test_deduplication(self):
        """Test requesting same hash multiple times."""
        backend = MockGCSBackend()
        h1 = backend.write_content(b"dedup").content_id

        result = backend.batch_read_content([h1, h1, h1])

        # All three entries should be the same content
        assert len(result) == 1  # dict deduplicates keys
        assert result[h1] == b"dedup"


# === S3 batch_read_content tests (#1626) ===


class MockS3ConnectorForBatch(PathAddressingEngine):
    """Mock S3-like connector for testing batch_read_content with per-file contexts.

    Simulates path-based access where each file needs its own OperationContext.
    """

    batch_read_workers: int = 4  # Low for tests

    def __init__(self) -> None:
        transport = InMemoryTransport()
        super().__init__(transport, backend_name="s3_connector", bucket_name="test-bucket")
        self.read_count: int = 0
        self.session_factory = None

    @property
    def files(self) -> dict[str, bytes]:
        """Proxy to transport.files for test manipulation."""
        return self._transport.files

    @files.setter
    def files(self, value: dict[str, bytes]) -> None:
        self._transport.files = value

    def read_content(self, content_id, context=None) -> bytes:
        """S3-style read that requires context.backend_path."""
        self.read_count += 1
        if not context or not context.backend_path:
            raise BackendError(
                "S3 connector requires backend_path",
                backend=self.name,
            )
        blob_path = self._get_key_path(context.backend_path)
        if blob_path not in self._transport.files:
            raise NexusFileNotFoundError(blob_path)
        return self._transport.files[blob_path]

    def batch_read_content(
        self, content_ids, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        """S3-style batch read with per-file contexts (mirrors S3ConnectorBackend)."""
        if not content_ids:
            return {}

        result: dict[str, bytes | None] = {}

        if len(content_ids) == 1:
            ctx = contexts.get(content_ids[0], context) if contexts else context
            try:
                data = self.read_content(content_ids[0], context=ctx)
                return {content_ids[0]: data}
            except (NexusFileNotFoundError, BackendError):
                return {content_ids[0]: None}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(self.batch_read_workers, len(content_ids))

        def read_one(content_id: str) -> tuple[str, bytes | None]:
            ctx = contexts.get(content_id, context) if contexts else context
            try:
                data = self.read_content(content_id, context=ctx)
                return (content_id, data)
            except (NexusFileNotFoundError, BackendError):
                return (content_id, None)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(read_one, h): h for h in content_ids}
            for future in as_completed(futures):
                hash_key, file_content = future.result()
                result[hash_key] = file_content

        return result


class TestS3BatchReadContent:
    """Test S3-style batch_read_content with per-file contexts."""

    def _make_context(self, backend_path: str) -> OperationContext:
        """Create a minimal OperationContext with backend_path."""
        ctx = MagicMock(spec=OperationContext)
        ctx.backend_path = backend_path
        ctx.virtual_path = None
        ctx.zone_id = None
        return ctx

    def test_basic_batch_read_with_contexts(self):
        """Test reading multiple S3 objects with per-file contexts."""
        backend = MockS3ConnectorForBatch()
        backend.files = {
            "data/file1.txt": b"content1",
            "data/file2.txt": b"content2",
            "data/file3.txt": b"content3",
        }

        contexts = {
            "hash1": self._make_context("data/file1.txt"),
            "hash2": self._make_context("data/file2.txt"),
            "hash3": self._make_context("data/file3.txt"),
        }

        result = backend.batch_read_content(["hash1", "hash2", "hash3"], contexts=contexts)

        assert result["hash1"] == b"content1"
        assert result["hash2"] == b"content2"
        assert result["hash3"] == b"content3"

    def test_missing_context_returns_none(self):
        """Test that hashes without contexts (and no shared context) return None."""
        backend = MockS3ConnectorForBatch()
        backend.files = {"data/file1.txt": b"content1"}

        contexts = {
            "hash1": self._make_context("data/file1.txt"),
        }

        # hash2 has no context and no fallback -- read_content will fail
        result = backend.batch_read_content(["hash1", "hash2"], contexts=contexts)

        assert result["hash1"] == b"content1"
        assert result["hash2"] is None  # Failed due to missing backend_path

    def test_shared_context_fallback(self):
        """Test that shared context is used when per-hash context is missing."""
        backend = MockS3ConnectorForBatch()
        backend.files = {"data/file1.txt": b"content1"}

        shared_ctx = self._make_context("data/file1.txt")

        # hash1 uses shared context (no per-hash contexts provided)
        result = backend.batch_read_content(["hash1"], context=shared_ctx)

        assert result["hash1"] == b"content1"

    def test_empty_list_returns_empty_dict(self):
        """Test empty input returns empty dict."""
        backend = MockS3ConnectorForBatch()
        assert backend.batch_read_content([]) == {}

    def test_single_item_skips_thread_pool(self):
        """Test single-item optimization."""
        backend = MockS3ConnectorForBatch()
        backend.files = {"data/single.txt": b"single"}

        contexts = {"hash1": self._make_context("data/single.txt")}
        result = backend.batch_read_content(["hash1"], contexts=contexts)

        assert result["hash1"] == b"single"
        assert backend.read_count == 1

    def test_partial_failures_with_mixed_contexts(self):
        """Test mix of successful and failed reads in parallel."""
        backend = MockS3ConnectorForBatch()
        backend.files = {
            "data/file1.txt": b"content1",
            # file2.txt doesn't exist
            "data/file3.txt": b"content3",
        }

        contexts = {
            "hash1": self._make_context("data/file1.txt"),
            "hash2": self._make_context("data/file2.txt"),  # Missing file
            "hash3": self._make_context("data/file3.txt"),
        }

        result = backend.batch_read_content(["hash1", "hash2", "hash3"], contexts=contexts)

        assert result["hash1"] == b"content1"
        assert result["hash2"] is None  # File doesn't exist
        assert result["hash3"] == b"content3"

    def test_parallel_execution_with_many_files(self):
        """Test parallel reads with 20 files."""
        backend = MockS3ConnectorForBatch()
        contexts = {}
        for i in range(20):
            backend.files[f"data/file{i}.txt"] = f"content{i}".encode()
            contexts[f"hash{i}"] = self._make_context(f"data/file{i}.txt")

        hashes = [f"hash{i}" for i in range(20)]
        backend.read_count = 0

        result = backend.batch_read_content(hashes, contexts=contexts)

        assert len(result) == 20
        assert backend.read_count == 20
        assert all(v is not None for v in result.values())


# === Backend base class contexts parameter tests (#1626) ===


class TestBatchReadContentContextsParam:
    """Test the new contexts parameter on Backend.batch_read_content."""

    def test_default_impl_uses_per_hash_context(self):
        """Test that default batch_read_content uses per-hash contexts."""
        backend = MockS3ConnectorForBatch()
        backend.files = {
            "path_a.txt": b"content_a",
            "path_b.txt": b"content_b",
        }

        ctx_a = MagicMock(spec=OperationContext)
        ctx_a.backend_path = "path_a.txt"
        ctx_b = MagicMock(spec=OperationContext)
        ctx_b.backend_path = "path_b.txt"

        # Use the base class default (sequential) with per-hash contexts
        result = Backend.batch_read_content(
            backend,
            ["hash_a", "hash_b"],
            contexts={"hash_a": ctx_a, "hash_b": ctx_b},
        )

        assert result["hash_a"] == b"content_a"
        assert result["hash_b"] == b"content_b"

    def test_default_impl_falls_back_to_shared_context(self):
        """Test that shared context is used when hash not in contexts dict."""
        backend = MockS3ConnectorForBatch()
        backend.files = {"shared_path.txt": b"shared_content"}

        shared_ctx = MagicMock(spec=OperationContext)
        shared_ctx.backend_path = "shared_path.txt"

        # Only hash1 in contexts, hash2 should fall back to shared context
        ctx_for_hash1 = MagicMock(spec=OperationContext)
        ctx_for_hash1.backend_path = "shared_path.txt"

        result = Backend.batch_read_content(
            backend,
            ["hash1", "hash2"],
            context=shared_ctx,
            contexts={"hash1": ctx_for_hash1},
        )

        assert result["hash1"] == b"shared_content"
        assert result["hash2"] == b"shared_content"

    def test_backward_compat_no_contexts(self):
        """Test that batch_read_content still works without contexts param."""
        backend = MockGCSBackend()
        h1 = backend.write_content(b"compat").content_id

        # Call without contexts (backward compatible)
        result = backend.batch_read_content([h1])

        assert result[h1] == b"compat"
