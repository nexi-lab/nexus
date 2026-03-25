"""Unit tests for CASAddressingEngine — CAS addressing over InMemoryBlobTransport.

Tests cover:
- Content-addressable write/read/delete with hash-based paths
- Reference counting (ref_count increment on dup, decrement on delete)
- Hash integrity verification on read
- Directory operations (mkdir, rmdir, is_directory, list_dir)
- Streaming (stream_content, write_stream)
- Batch operations (batch_read_content)
- Error handling (missing content, corrupt metadata)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from collections.abc import Iterator

import pytest

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

# === InMemoryBlobTransport ===


class InMemoryBlobTransport:
    """Minimal in-memory BlobTransport for testing."""

    transport_name: str = "memory"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def put_blob(self, key: str, data: bytes, content_type: str = "") -> str | None:
        self.files[key] = data
        return None

    def get_blob(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        if key not in self.files:
            raise NexusFileNotFoundError(key)
        return self.files[key], None

    def delete_blob(self, key: str) -> None:
        if key not in self.files:
            raise NexusFileNotFoundError(key)
        del self.files[key]

    def blob_exists(self, key: str) -> bool:
        return key in self.files

    def get_blob_size(self, key: str) -> int:
        if key not in self.files:
            raise NexusFileNotFoundError(key)
        return len(self.files[key])

    def list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        blob_keys = [k for k in self.files if k.startswith(prefix)]
        common_prefixes: list[str] = []
        if delimiter:
            seen: set[str] = set()
            for k in blob_keys:
                rest = k[len(prefix) :]
                if delimiter in rest:
                    pfx = prefix + rest[: rest.index(delimiter) + 1]
                    seen.add(pfx)
            common_prefixes = sorted(seen)
            # Only return blobs that don't have delimiter after prefix
            blob_keys = [k for k in blob_keys if delimiter not in k[len(prefix) :]]
        return sorted(blob_keys), common_prefixes

    def copy_blob(self, src_key: str, dst_key: str) -> None:
        if src_key not in self.files:
            raise NexusFileNotFoundError(src_key)
        self.files[dst_key] = self.files[src_key]

    def create_directory_marker(self, key: str) -> None:
        self.files[key] = b""

    def stream_blob(
        self, key: str, chunk_size: int = 8192, version_id: str | None = None
    ) -> Iterator[bytes]:
        if key not in self.files:
            raise NexusFileNotFoundError(key)
        data = self.files[key]
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


# === Fixtures ===


@pytest.fixture
def transport() -> InMemoryBlobTransport:
    return InMemoryBlobTransport()


@pytest.fixture
def backend(transport: InMemoryBlobTransport) -> CASAddressingEngine:
    return CASAddressingEngine(transport, backend_name="test-cas")


# === Test Classes ===


class TestCASAddressingEngineWriteContent:
    """Test write_content() — CAS dedup and ref counting."""

    def test_write_stores_at_cas_path(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        content = b"hello world"
        h = hash_content(content)
        result = backend.write_content(content)

        assert result.content_id == h
        assert result.size == len(content)

        # Blob stored at cas/xx/yy/hash
        cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
        assert cas_key in transport.files
        assert transport.files[cas_key] == content

    def test_write_creates_metadata_sidecar(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        content = b"test metadata"
        h = hash_content(content)
        backend.write_content(content)

        meta_key = f"cas/{h[:2]}/{h[2:4]}/{h}.meta"
        assert meta_key in transport.files

        import json

        meta = json.loads(transport.files[meta_key])
        assert meta["ref_count"] == 1
        assert meta["size"] == len(content)

    def test_write_dedup_increments_ref_count(self, backend: CASAddressingEngine):
        content = b"duplicate content"

        r1 = backend.write_content(content)
        r2 = backend.write_content(content)

        assert r1.content_id == r2.content_id
        assert backend.get_ref_count(r1.content_id) == 2

    def test_write_different_content_different_hashes(self, backend: CASAddressingEngine):
        r1 = backend.write_content(b"content A")
        r2 = backend.write_content(b"content B")

        assert r1.content_id != r2.content_id

    def test_write_returns_write_result(self, backend: CASAddressingEngine):
        result = backend.write_content(b"test")
        assert isinstance(result, WriteResult)


class TestCASAddressingEngineReadContent:
    """Test read_content() — hash-based retrieval and integrity verification."""

    def test_read_returns_content(self, backend: CASAddressingEngine):
        content = b"read me"
        result = backend.write_content(content)
        data = backend.read_content(result.content_id)
        assert data == content

    def test_read_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.read_content("a" * 64)

    def test_read_verifies_hash_integrity(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        content = b"original"
        result = backend.write_content(content)

        # Corrupt the stored blob
        cas_key = f"cas/{result.content_id[:2]}/{result.content_id[2:4]}/{result.content_id}"
        transport.files[cas_key] = b"corrupted"

        with pytest.raises(BackendError, match="hash mismatch"):
            backend.read_content(result.content_id)


class TestCASAddressingEngineDeleteContent:
    """Test delete_content() — ref counting and cleanup."""

    def test_delete_removes_blob_on_last_ref(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        content = b"delete me"
        result = backend.write_content(content)
        h = result.content_id

        backend.delete_content(h)

        cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
        assert cas_key not in transport.files

    def test_delete_decrements_ref_count(self, backend: CASAddressingEngine):
        content = b"ref counted"
        result = backend.write_content(content)
        backend.write_content(content)  # ref_count = 2

        backend.delete_content(result.content_id)
        assert backend.get_ref_count(result.content_id) == 1

    def test_delete_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.delete_content("b" * 64)

    def test_delete_cleans_up_meta_sidecar(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        content = b"cleanup"
        result = backend.write_content(content)
        h = result.content_id

        backend.delete_content(h)

        meta_key = f"cas/{h[:2]}/{h[2:4]}/{h}.meta"
        assert meta_key not in transport.files


class TestCASAddressingEngineContentOperations:
    """Test content_exists, get_content_size, get_ref_count."""

    def test_content_exists_true(self, backend: CASAddressingEngine):
        result = backend.write_content(b"exists")
        assert backend.content_exists(result.content_id) is True

    def test_content_exists_false(self, backend: CASAddressingEngine):
        assert backend.content_exists("c" * 64) is False

    def test_get_content_size(self, backend: CASAddressingEngine):
        content = b"size check"
        result = backend.write_content(content)
        assert backend.get_content_size(result.content_id) == len(content)

    def test_get_content_size_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.get_content_size("d" * 64)

    def test_get_ref_count_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.get_ref_count("e" * 64)


class TestCASAddressingEngineStreaming:
    """Test stream_content and write_stream."""

    def test_stream_content_yields_chunks(self, backend: CASAddressingEngine):
        content = b"A" * 100
        result = backend.write_content(content)

        chunks = list(backend.stream_content(result.content_id, chunk_size=30))
        assert b"".join(chunks) == content

    def test_stream_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            list(backend.stream_content("f" * 64))

    def test_write_stream(self, backend: CASAddressingEngine):
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        result = backend.write_stream(iter(chunks))

        data = backend.read_content(result.content_id)
        assert data == b"chunk1chunk2chunk3"


class TestCASAddressingEngineBatchRead:
    """Test batch_read_content."""

    def test_batch_read_multiple(self, backend: CASAddressingEngine):
        h1 = backend.write_content(b"file1").content_id
        h2 = backend.write_content(b"file2").content_id
        h3 = backend.write_content(b"file3").content_id

        result = backend.batch_read_content([h1, h2, h3])

        assert result[h1] == b"file1"
        assert result[h2] == b"file2"
        assert result[h3] == b"file3"

    def test_batch_read_empty(self, backend: CASAddressingEngine):
        assert backend.batch_read_content([]) == {}

    def test_batch_read_single_optimization(self, backend: CASAddressingEngine):
        h = backend.write_content(b"single").content_id
        result = backend.batch_read_content([h])
        assert result[h] == b"single"

    def test_batch_read_partial_failures(self, backend: CASAddressingEngine):
        h = backend.write_content(b"exists").content_id
        fake = "a" * 64

        result = backend.batch_read_content([h, fake])

        assert result[h] == b"exists"
        assert result[fake] is None


class TestCASAddressingEngineDirectories:
    """Test directory operations (CAS uses dirs/ prefix)."""

    def test_mkdir_creates_marker(
        self, backend: CASAddressingEngine, transport: InMemoryBlobTransport
    ):
        backend.mkdir("data")
        assert "dirs/data/" in transport.files

    def test_mkdir_root_noop(self, backend: CASAddressingEngine, transport: InMemoryBlobTransport):
        backend.mkdir("")
        # Root always exists, no marker created
        assert not any(k.startswith("dirs/") for k in transport.files)

    def test_mkdir_exist_ok(self, backend: CASAddressingEngine):
        backend.mkdir("data")
        backend.mkdir("data", exist_ok=True)  # No error

    def test_mkdir_duplicate_raises(self, backend: CASAddressingEngine):
        backend.mkdir("data")
        with pytest.raises(FileExistsError):
            backend.mkdir("data")

    def test_is_directory(self, backend: CASAddressingEngine):
        assert backend.is_directory("") is True  # Root
        assert backend.is_directory("nonexistent") is False
        backend.mkdir("data")
        assert backend.is_directory("data") is True

    def test_rmdir(self, backend: CASAddressingEngine, transport: InMemoryBlobTransport):
        backend.mkdir("data")
        backend.rmdir("data")
        assert "dirs/data/" not in transport.files

    def test_rmdir_missing_raises(self, backend: CASAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.rmdir("nonexistent")

    def test_rmdir_root_raises(self, backend: CASAddressingEngine):
        with pytest.raises(BackendError, match="root"):
            backend.rmdir("")


class TestCASAddressingEngineName:
    """Test name property and default name generation."""

    def test_custom_name(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="my-cas")
        assert backend.name == "my-cas"

    def test_default_name(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport)
        assert backend.name == "cas-memory"


class TestVerifyOnRead:
    """Test verify_on_read flag — configurable integrity hash on read."""

    def test_verify_on_read_true_detects_corruption(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="test", verify_on_read=True)
        content = b"original"
        result = backend.write_content(content)
        # Corrupt stored blob
        cas_key = f"cas/{result.content_id[:2]}/{result.content_id[2:4]}/{result.content_id}"
        transport.files[cas_key] = b"corrupted"
        with pytest.raises(BackendError, match="hash mismatch"):
            backend.read_content(result.content_id)

    def test_verify_on_read_false_skips_hash(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="test", verify_on_read=False)
        content = b"original"
        result = backend.write_content(content)
        # Corrupt stored blob
        cas_key = f"cas/{result.content_id[:2]}/{result.content_id[2:4]}/{result.content_id}"
        transport.files[cas_key] = b"corrupted"
        # Should return corrupted data without raising
        data = backend.read_content(result.content_id)
        assert data == b"corrupted"

    def test_verify_on_read_default_is_true(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="test")
        assert backend._verify_on_read is True


class TestDedupSkip:
    """Test dedup skip — blob_exists check before put_blob on write."""

    def test_dedup_write_increments_ref_count(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="test")
        content = b"dedup content"
        r1 = backend.write_content(content)
        r2 = backend.write_content(content)
        assert r1.content_id == r2.content_id
        assert backend.get_ref_count(r1.content_id) == 2

    def test_dedup_write_skips_put_blob(self, transport: InMemoryBlobTransport):
        """On dedup, put_blob should not be called the second time."""
        from unittest.mock import patch

        backend = CASAddressingEngine(transport, backend_name="test")
        content = b"dedup test"
        backend.write_content(content)

        # Track put_blob calls via mock wrapper
        h = hash_content(content)
        content_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
        original_put = transport.put_blob
        put_calls: list[str] = []

        def tracking_put(key: str, data: bytes, content_type: str = "") -> str | None:
            put_calls.append(key)
            return original_put(key, data, content_type)

        with patch.object(transport, "put_blob", side_effect=tracking_put):
            backend.write_content(content)  # second write = dedup

        # put_blob should NOT have been called for the content blob
        assert content_key not in put_calls

    def test_fresh_write_calls_put_blob(self, transport: InMemoryBlobTransport):
        backend = CASAddressingEngine(transport, backend_name="test")
        content = b"fresh content"
        result = backend.write_content(content)
        h = result.content_id
        cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
        assert cas_key in transport.files
        assert transport.files[cas_key] == content


class TestNosyncMetaDispatch:
    """Test _write_meta uses put_blob_nosync when available."""

    def test_meta_uses_nosync_when_available(self):
        """When transport has put_blob_nosync, _write_meta should use it."""

        class NosyncTransport(InMemoryBlobTransport):
            """Transport with put_blob_nosync support."""

            def __init__(self) -> None:
                super().__init__()
                self.nosync_calls: list[str] = []

            def put_blob_nosync(self, key: str, data: bytes) -> None:
                self.nosync_calls.append(key)
                self.files[key] = data

        transport = NosyncTransport()
        backend = CASAddressingEngine(transport, backend_name="test")
        backend.write_content(b"test nosync meta")
        # Meta sidecar should have been written via nosync
        assert any(k.endswith(".meta") for k in transport.nosync_calls)

    def test_meta_falls_back_to_put_blob(self, transport: InMemoryBlobTransport):
        """Without put_blob_nosync, meta goes through regular put_blob."""
        assert not hasattr(transport, "put_blob_nosync")
        backend = CASAddressingEngine(transport, backend_name="test")
        result = backend.write_content(b"fallback meta")
        h = result.content_id
        meta_key = f"cas/{h[:2]}/{h[2:4]}/{h}.meta"
        assert meta_key in transport.files
