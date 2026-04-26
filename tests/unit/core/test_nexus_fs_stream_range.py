"""Tests for NexusFS stream_range, read_range, and write_stream.

Issue #1519, 9A: Covers byte-range reading, streaming, and writing
with validation and error handling. Tests the methods directly
via a lightweight stub to avoid the heavy NexusFS constructor.
"""

import contextlib
from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.types import OperationContext


class _StubFS:
    """Lightweight stub that provides just enough for stream/range testing."""

    def __init__(self, backend, metadata, router):
        self.backend = backend
        self.metadata = metadata
        self.router = router
        self._enforce_permissions = False
        self._zone_id = ROOT_ZONE_ID
        self._init_cred = OperationContext(user_id="test", groups=[], zone_id=ROOT_ZONE_ID)
        # DispatchMixin stub — pre-hooks dispatched via Rust kernel
        self.read_hook_count = 0
        self.resolve_read = MagicMock(return_value=(False, None))
        self._kernel = MagicMock()
        self._kernel.dispatch_pre_hooks = MagicMock()
        # Kernel IPC primitives — empty registries (no pipes/streams in range tests)
        self._pipe_manager = None
        self._stream_manager = MagicMock()
        self._stream_manager._buffers = {}
        # DriverLifecycleCoordinator stub (routing is Rust-owned; keep ref for stub sys_read)
        self._driver_coordinator = MagicMock()
        self._test_backend = backend

    def _validate_path(self, path):
        if not path.startswith("/"):
            from nexus.contracts.exceptions import InvalidPathError

            raise InvalidPathError(path)
        return path

    def _get_context_identity(self, context):
        return "default", None, False

    def _parse_context(self, context):
        return context

    @contextlib.contextmanager
    def _vfs_locked(self, path, mode):
        yield

    def sys_read(self, path, *, count=None, offset=0, context=None):
        """Stub sys_read for read_range and stream_range fallback path."""
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)
        content = self._test_backend.read_content(meta.etag or "")
        if offset:
            content = content[offset:]
        if count is not None:
            content = content[:count]
        return content


# Graft VFS methods onto stub (Issue #899: dissolved from mixin into NexusFS)
from nexus.core.nexus_fs import NexusFS  # noqa: E402

_StubFS.read_range = NexusFS.read_range
_StubFS.stream_range = NexusFS.stream_range


@pytest.fixture()
def stub_fs():
    """Create a lightweight stub with mocked backends for range testing."""
    backend = MagicMock()
    backend.read_content.return_value = b"Hello, World! This is test content."
    backend.stream_range.return_value = iter([b"Hello", b", Wor", b"ld!"])
    # Prevent MagicMock hasattr from matching read_content_range
    del backend.read_content_range
    meta_entry = MagicMock()
    meta_entry.etag = "sha256:abc123"
    meta_entry.size = 34
    metadata = MagicMock()
    metadata.get.return_value = meta_entry

    route = MagicMock()
    route.backend = backend
    route.metastore = metadata  # route.metastore == self.metadata (same mock)
    route.backend_path = "/test"
    route.readonly = False
    router = MagicMock()
    router.route.return_value = route

    stub = _StubFS(backend=backend, metadata=metadata, router=router)
    # F3 C6b: read_range / stream / stream_range now fetch metadata via
    # ``self._kernel.metastore_get`` so the per-zone ``ZoneMetastore`` in
    # mount_table is honoured (was ``route.metastore.get``, which only
    # saw the root-zone store in federation mode). Wire the kernel mock
    # at the same target so the tests that poke ``metadata.get`` keep
    # working without duplicating fixture setup.
    stub._kernel.metastore_get = metadata.get
    return stub


class TestReadRange:
    """Tests for read_range() byte-range reading."""

    @pytest.mark.asyncio
    def test_returns_correct_slice(self, stub_fs):
        result = stub_fs.read_range("/test/file.txt", 0, 5)
        assert result == b"Hello"

    @pytest.mark.asyncio
    def test_middle_slice(self, stub_fs):
        result = stub_fs.read_range("/test/file.txt", 7, 12)
        assert result == b"World"

    @pytest.mark.asyncio
    def test_empty_when_start_equals_end(self, stub_fs):
        result = stub_fs.read_range("/test/file.txt", 5, 5)
        assert result == b""

    @pytest.mark.asyncio
    def test_negative_start_raises_value_error(self, stub_fs):
        with pytest.raises(ValueError, match="start must be non-negative"):
            stub_fs.read_range("/test/file.txt", -1, 10)

    @pytest.mark.asyncio
    def test_end_less_than_start_raises_value_error(self, stub_fs):
        with pytest.raises(ValueError, match="end.*must be >= start"):
            stub_fs.read_range("/test/file.txt", 10, 5)

    @pytest.mark.asyncio
    def test_file_not_found_returns_error(self, stub_fs):
        stub_fs.metadata.get.return_value = None
        with pytest.raises(NexusFileNotFoundError):
            stub_fs.read_range("/test/missing.txt", 0, 10)

    @pytest.mark.asyncio
    def test_file_with_no_etag_reads_via_sys_read(self, stub_fs):
        """Post-simplification: read_range delegates to sys_read, which reads
        via backend regardless of etag presence.  A file with metadata but
        no etag still returns content (the backend call uses the etag value
        as content_id, and the mock backend returns canned content)."""
        meta = MagicMock()
        meta.etag = None
        stub_fs.metadata.get.return_value = meta
        # sys_read stub calls backend.read_content(meta.etag or "")
        # which returns the default mock return value
        result = stub_fs.read_range("/test/empty.txt", 0, 10)
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    def test_beyond_content_returns_truncated(self, stub_fs):
        """Reading beyond file size returns available bytes."""
        content = b"Hello, World! This is test content."
        result = stub_fs.read_range("/test/file.txt", 30, 1000)
        assert result == content[30:]  # Python slice handles out-of-bounds

    @pytest.mark.asyncio
    def test_full_range_returns_all_content(self, stub_fs):
        content = b"Hello, World! This is test content."
        result = stub_fs.read_range("/test/file.txt", 0, len(content))
        assert result == content

    @pytest.mark.asyncio
    def test_zero_to_zero_returns_empty(self, stub_fs):
        result = stub_fs.read_range("/test/file.txt", 0, 0)
        assert result == b""


class TestStreamRange:
    """Tests for stream_range() chunked byte-range streaming.

    Since §12, stream_range delegates to sys_read(offset, count) and yields
    chunks in Python — no direct backend.stream_range call.
    """

    def test_yields_correct_content(self, stub_fs):
        """stream_range returns the correct byte range, chunked."""
        chunks = list(stub_fs.stream_range("/test/file.txt", 0, 12, chunk_size=5))
        combined = b"".join(chunks)
        assert combined == b"Hello, World!"

    def test_file_not_found_raises(self, stub_fs):
        stub_fs.metadata.get.return_value = None
        with pytest.raises(NexusFileNotFoundError):
            list(stub_fs.stream_range("/test/missing.txt", 0, 10))

    def test_respects_chunk_size(self, stub_fs):
        """Chunks are at most chunk_size bytes."""
        chunks = list(stub_fs.stream_range("/test/file.txt", 0, 12, chunk_size=5))
        for chunk in chunks[:-1]:
            assert len(chunk) == 5
        assert len(chunks[-1]) <= 5

    def test_correct_range_offset(self, stub_fs):
        """stream_range with non-zero start returns correct slice."""
        chunks = list(stub_fs.stream_range("/test/file.txt", 7, 11))
        combined = b"".join(chunks)
        assert combined == b"World"
