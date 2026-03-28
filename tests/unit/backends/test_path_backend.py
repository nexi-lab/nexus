"""Unit tests for PathAddressingEngine — path addressing over InMemoryTransport.

Tests cover:
- Path-based write/read/delete (requires OperationContext with backend_path)
- Content-Type detection
- Directory operations (mkdir, rmdir, is_directory, list_dir)
- Streaming (stream_content)
- Batch operations (batch_read_content, batch_get_versions)
- Prefix handling
- Rename file (copy + delete)
- Bulk download (_bulk_download_blobs for BackendIOService compat)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.contracts.types import OperationContext
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

# === InMemoryTransport ===


class InMemoryTransport:
    """Minimal in-memory Transport for testing."""

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


# === Helpers ===


def _make_context(backend_path: str, virtual_path: str | None = None) -> OperationContext:
    ctx = MagicMock(spec=OperationContext)
    ctx.backend_path = backend_path
    ctx.virtual_path = virtual_path
    ctx.zone_id = None
    return ctx


# === Fixtures ===


@pytest.fixture
def transport() -> InMemoryTransport:
    return InMemoryTransport()


@pytest.fixture
def backend(transport: InMemoryTransport) -> PathAddressingEngine:
    return PathAddressingEngine(transport, backend_name="test-path", bucket_name="test-bucket")


@pytest.fixture
def prefixed_backend(transport: InMemoryTransport) -> PathAddressingEngine:
    return PathAddressingEngine(
        transport, backend_name="test-prefixed", bucket_name="test-bucket", prefix="data"
    )


# === Test Classes ===


class TestPathAddressingEngineWriteContent:
    """Test write_content() — path-based storage."""

    def test_write_stores_at_backend_path(
        self, backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        ctx = _make_context("docs/file.txt")
        result = backend.write_content(b"hello world", context=ctx)

        assert isinstance(result, WriteResult)
        assert result.size == 11
        assert "docs/file.txt" in transport.files
        assert transport.files["docs/file.txt"] == b"hello world"

    def test_write_requires_backend_path(self, backend: PathAddressingEngine):
        with pytest.raises(BackendError, match="requires content_id or backend_path"):
            backend.write_content(b"no context")

    def test_write_requires_context(self, backend: PathAddressingEngine):
        with pytest.raises(BackendError, match="requires content_id or backend_path"):
            backend.write_content(b"no path", context=_make_context(""))

    def test_write_with_prefix(
        self, prefixed_backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        ctx = _make_context("file.txt")
        prefixed_backend.write_content(b"prefixed", context=ctx)

        assert "data/file.txt" in transport.files

    def test_write_computes_hash(self, backend: PathAddressingEngine):
        content = b"hash me"
        ctx = _make_context("file.txt")
        result = backend.write_content(content, context=ctx)

        expected = hash_content(content)
        assert result.content_id == expected


class TestPathAddressingEngineReadContent:
    """Test read_content() — path-based retrieval."""

    def test_read_returns_content(self, backend: PathAddressingEngine):
        ctx = _make_context("file.txt")
        backend.write_content(b"read me", context=ctx)

        data = backend.read_content("any-hash", context=ctx)
        assert data == b"read me"

    def test_read_requires_backend_path(self, backend: PathAddressingEngine):
        with pytest.raises(BackendError, match="requires backend_path"):
            backend.read_content("hash")

    def test_read_missing_raises(self, backend: PathAddressingEngine):
        ctx = _make_context("nonexistent.txt")
        with pytest.raises(NexusFileNotFoundError):
            backend.read_content("hash", context=ctx)


class TestPathAddressingEngineDeleteContent:
    """Test delete_content()."""

    def test_delete_removes_blob(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        ctx = _make_context("file.txt")
        backend.write_content(b"delete me", context=ctx)
        assert "file.txt" in transport.files

        backend.delete_content("hash", context=ctx)
        assert "file.txt" not in transport.files

    def test_delete_requires_backend_path(self, backend: PathAddressingEngine):
        with pytest.raises(BackendError, match="requires backend_path"):
            backend.delete_content("hash")

    def test_delete_missing_raises(self, backend: PathAddressingEngine):
        ctx = _make_context("missing.txt")
        with pytest.raises(NexusFileNotFoundError):
            backend.delete_content("hash", context=ctx)


class TestPathAddressingEngineContentOps:
    """Test content_exists, get_content_size."""

    def test_content_exists(self, backend: PathAddressingEngine):
        ctx = _make_context("file.txt")
        backend.write_content(b"exists", context=ctx)

        assert backend.content_exists("hash", context=ctx) is True
        assert backend.content_exists("hash", context=_make_context("nope.txt")) is False

    def test_content_exists_no_context(self, backend: PathAddressingEngine):
        assert backend.content_exists("hash") is False

    def test_get_content_size(self, backend: PathAddressingEngine):
        ctx = _make_context("file.txt")
        backend.write_content(b"size test", context=ctx)
        assert backend.get_content_size("hash", context=ctx) == 9


class TestPathAddressingEngineStreaming:
    """Test stream_content."""

    def test_stream_yields_chunks(self, backend: PathAddressingEngine):
        ctx = _make_context("file.txt")
        backend.write_content(b"A" * 100, context=ctx)

        chunks = list(backend.stream_content("hash", chunk_size=30, context=ctx))
        assert b"".join(chunks) == b"A" * 100

    def test_stream_requires_backend_path(self, backend: PathAddressingEngine):
        with pytest.raises(ValueError, match="requires backend_path"):
            list(backend.stream_content("hash"))


class TestPathAddressingEngineBatchRead:
    """Test batch_read_content."""

    def test_batch_read_multiple(self, backend: PathAddressingEngine):
        ctx1 = _make_context("file1.txt")
        ctx2 = _make_context("file2.txt")
        ctx3 = _make_context("file3.txt")
        backend.write_content(b"content1", context=ctx1)
        backend.write_content(b"content2", context=ctx2)
        backend.write_content(b"content3", context=ctx3)

        result = backend.batch_read_content(
            ["h1", "h2", "h3"],
            contexts={"h1": ctx1, "h2": ctx2, "h3": ctx3},
        )

        assert result["h1"] == b"content1"
        assert result["h2"] == b"content2"
        assert result["h3"] == b"content3"

    def test_batch_read_empty(self, backend: PathAddressingEngine):
        assert backend.batch_read_content([]) == {}

    def test_batch_read_single(self, backend: PathAddressingEngine):
        ctx = _make_context("file.txt")
        backend.write_content(b"single", context=ctx)

        result = backend.batch_read_content(["h"], context=ctx)
        assert result["h"] == b"single"

    def test_batch_read_partial_failures(self, backend: PathAddressingEngine):
        ctx1 = _make_context("file1.txt")
        ctx2 = _make_context("missing.txt")
        backend.write_content(b"exists", context=ctx1)

        result = backend.batch_read_content(["h1", "h2"], contexts={"h1": ctx1, "h2": ctx2})

        assert result["h1"] == b"exists"
        assert result["h2"] is None


class TestPathAddressingEngineDirectories:
    """Test directory operations."""

    def test_mkdir(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        backend.mkdir("data")
        assert "data/" in transport.files

    def test_mkdir_with_prefix(
        self, prefixed_backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        prefixed_backend.mkdir("subdir")
        assert "data/subdir/" in transport.files

    def test_mkdir_root_noop(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        backend.mkdir("")
        assert not any(k.endswith("/") for k in transport.files)

    def test_mkdir_exist_ok(self, backend: PathAddressingEngine):
        backend.mkdir("data")
        backend.mkdir("data", exist_ok=True)

    def test_mkdir_duplicate_raises(self, backend: PathAddressingEngine):
        backend.mkdir("data")
        with pytest.raises(BackendError, match="already exists"):
            backend.mkdir("data")

    def test_is_directory(self, backend: PathAddressingEngine):
        assert backend.is_directory("") is True
        assert backend.is_directory("nonexistent") is False
        backend.mkdir("data")
        assert backend.is_directory("data") is True

    def test_rmdir(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        backend.mkdir("data")
        backend.rmdir("data")
        assert "data/" not in transport.files

    def test_rmdir_missing_raises(self, backend: PathAddressingEngine):
        with pytest.raises(NexusFileNotFoundError):
            backend.rmdir("nonexistent")

    def test_rmdir_root_raises(self, backend: PathAddressingEngine):
        with pytest.raises(BackendError, match="root"):
            backend.rmdir("")


class TestPathAddressingEngineRename:
    """Test rename_file (copy + delete)."""

    def test_rename(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        transport.files["old.txt"] = b"content"

        backend.rename_file("old.txt", "new.txt")

        assert "old.txt" not in transport.files
        assert transport.files["new.txt"] == b"content"

    def test_rename_source_missing_raises(self, backend: PathAddressingEngine):
        with pytest.raises(FileNotFoundError):
            backend.rename_file("missing.txt", "new.txt")

    def test_rename_dest_exists_raises(
        self, backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        transport.files["old.txt"] = b"old"
        transport.files["new.txt"] = b"new"

        with pytest.raises(FileExistsError):
            backend.rename_file("old.txt", "new.txt")


class TestPathAddressingEngineBulkDownload:
    """Test _bulk_download_blobs (BackendIOService compat)."""

    def test_bulk_download(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        transport.files["a.txt"] = b"content_a"
        transport.files["b.txt"] = b"content_b"

        result = backend._bulk_download_blobs(["a.txt", "b.txt"], max_workers=2)

        assert result["a.txt"] == b"content_a"
        assert result["b.txt"] == b"content_b"

    def test_bulk_download_empty(self, backend: PathAddressingEngine):
        assert backend._bulk_download_blobs([]) == {}

    def test_bulk_download_handles_failures(
        self, backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        transport.files["exists.txt"] = b"data"

        result = backend._bulk_download_blobs(["exists.txt", "missing.txt"], max_workers=2)

        assert result["exists.txt"] == b"data"
        assert "missing.txt" not in result


class TestPathAddressingEnginePrefix:
    """Test prefix handling in _get_blob_path."""

    def test_no_prefix(self, backend: PathAddressingEngine):
        assert backend._get_blob_path("file.txt") == "file.txt"
        assert backend._get_blob_path("dir/file.txt") == "dir/file.txt"

    def test_with_prefix(self, prefixed_backend: PathAddressingEngine):
        assert prefixed_backend._get_blob_path("file.txt") == "data/file.txt"
        assert prefixed_backend._get_blob_path("dir/file.txt") == "data/dir/file.txt"

    def test_strips_leading_slash(self, backend: PathAddressingEngine):
        assert backend._get_blob_path("/file.txt") == "file.txt"


class TestPathAddressingEngineName:
    """Test name property and default name generation."""

    def test_custom_name(self, transport: InMemoryTransport):
        backend = PathAddressingEngine(transport, backend_name="my-path")
        assert backend.name == "my-path"

    def test_default_name(self, transport: InMemoryTransport):
        backend = PathAddressingEngine(transport)
        assert backend.name == "path-memory"


class TestPathAddressingEngineOffsetWrite:
    """Test offset write (POSIX pwrite semantics) for PAS."""

    def test_offset_write_splice(self, backend: PathAddressingEngine, transport: InMemoryTransport):
        """Write at offset splices into existing content."""
        ctx = _make_context("file.txt")
        backend.write_content(b"Hello World", context=ctx)
        backend.write_content(b"Earth", offset=6, context=ctx)

        data = backend.read_content("", context=ctx)
        assert data == b"Hello Earth"

    def test_offset_write_beyond_eof_zero_fills(
        self, backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        """Offset beyond EOF zero-fills the gap."""
        ctx = _make_context("file.txt")
        backend.write_content(b"ABC", context=ctx)
        result = backend.write_content(b"XY", offset=5, context=ctx)

        data = backend.read_content("", context=ctx)
        assert data == b"ABC\x00\x00XY"
        assert result.size == 7

    def test_offset_zero_unchanged(
        self, backend: PathAddressingEngine, transport: InMemoryTransport
    ):
        """offset=0 (default) behaves as whole-file replace."""
        ctx = _make_context("file.txt")
        backend.write_content(b"original", context=ctx)
        backend.write_content(b"replaced", offset=0, context=ctx)

        data = backend.read_content("", context=ctx)
        assert data == b"replaced"
