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
        self._default_context = OperationContext(user_id="test", groups=[], zone_id=ROOT_ZONE_ID)
        self._dispatch = MagicMock()  # KernelDispatch stub — intercept_pre_* are no-ops
        self._dispatch.read_hook_count = 0
        self._dispatch.resolve_read.return_value = (False, None)
        self._overlay_resolver = None

    def _validate_path(self, path):
        if not path.startswith("/"):
            from nexus.contracts.exceptions import InvalidPathError

            raise InvalidPathError(path)
        return path

    def _get_routing_params(self, context):
        return "default", None, False

    def _parse_context(self, context):
        return context

    def _get_overlay_config(self, path):
        return None

    @contextlib.contextmanager
    def _vfs_locked(self, path, mode):
        yield


# Graft VFS methods onto stub (Issue #899: dissolved from mixin into NexusFS)
from nexus.core.nexus_fs import NexusFS  # noqa: E402

_StubFS.read_range = NexusFS.read_range
_StubFS._resolve_and_read = NexusFS._resolve_and_read
_StubFS.stream_range = NexusFS.stream_range


@pytest.fixture()
def stub_fs():
    """Create a lightweight stub with mocked backends for range testing."""
    backend = MagicMock()
    backend.read_content.return_value = b"Hello, World! This is test content."
    backend.stream_range.return_value = iter([b"Hello", b", Wor", b"ld!"])
    # Prevent MagicMock hasattr from matching read_content_range
    del backend.read_content_range
    # Stub needs explicit False for dynamic connector check
    backend.user_scoped = False
    backend.has_token_manager = False

    meta_entry = MagicMock()
    meta_entry.etag = "sha256:abc123"
    meta_entry.size = 34
    meta_entry.physical_path = "sha256:abc123"
    metadata = MagicMock()
    metadata.get.return_value = meta_entry

    route = MagicMock()
    route.backend = backend
    route.backend_path = "/test"
    route.readonly = False
    router = MagicMock()
    router.route.return_value = route

    return _StubFS(backend=backend, metadata=metadata, router=router)


class TestReadRange:
    """Tests for read_range() byte-range reading."""

    @pytest.mark.asyncio
    async def test_returns_correct_slice(self, stub_fs):
        result = await stub_fs.read_range("/test/file.txt", 0, 5)
        assert result == b"Hello"

    @pytest.mark.asyncio
    async def test_middle_slice(self, stub_fs):
        result = await stub_fs.read_range("/test/file.txt", 7, 12)
        assert result == b"World"

    @pytest.mark.asyncio
    async def test_empty_when_start_equals_end(self, stub_fs):
        result = await stub_fs.read_range("/test/file.txt", 5, 5)
        assert result == b""

    @pytest.mark.asyncio
    async def test_negative_start_raises_value_error(self, stub_fs):
        with pytest.raises(ValueError, match="start must be non-negative"):
            await stub_fs.read_range("/test/file.txt", -1, 10)

    @pytest.mark.asyncio
    async def test_end_less_than_start_raises_value_error(self, stub_fs):
        with pytest.raises(ValueError, match="end.*must be >= start"):
            await stub_fs.read_range("/test/file.txt", 10, 5)

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self, stub_fs):
        stub_fs.metadata.get.return_value = None
        with pytest.raises(NexusFileNotFoundError):
            await stub_fs.read_range("/test/missing.txt", 0, 10)

    @pytest.mark.asyncio
    async def test_file_with_no_etag_raises_not_found(self, stub_fs):
        meta = MagicMock()
        meta.etag = None
        stub_fs.metadata.get.return_value = meta
        with pytest.raises(NexusFileNotFoundError):
            await stub_fs.read_range("/test/empty.txt", 0, 10)

    @pytest.mark.asyncio
    async def test_beyond_content_returns_truncated(self, stub_fs):
        """Reading beyond file size returns available bytes."""
        content = b"Hello, World! This is test content."
        result = await stub_fs.read_range("/test/file.txt", 30, 1000)
        assert result == content[30:]  # Python slice handles out-of-bounds

    @pytest.mark.asyncio
    async def test_full_range_returns_all_content(self, stub_fs):
        content = b"Hello, World! This is test content."
        result = await stub_fs.read_range("/test/file.txt", 0, len(content))
        assert result == content

    @pytest.mark.asyncio
    async def test_zero_to_zero_returns_empty(self, stub_fs):
        result = await stub_fs.read_range("/test/file.txt", 0, 0)
        assert result == b""


class TestStreamRange:
    """Tests for stream_range() chunked byte-range streaming."""

    def test_yields_chunks(self, stub_fs):
        chunks = list(stub_fs.stream_range("/test/file.txt", 0, 12))
        assert chunks == [b"Hello", b", Wor", b"ld!"]

    def test_file_not_found_raises(self, stub_fs):
        stub_fs.metadata.get.return_value = None
        with pytest.raises(NexusFileNotFoundError):
            list(stub_fs.stream_range("/test/missing.txt", 0, 10))

    def test_passes_chunk_size_to_backend(self, stub_fs):
        list(stub_fs.stream_range("/test/file.txt", 0, 100, chunk_size=4096))
        backend = stub_fs.router.route.return_value.backend
        backend.stream_range.assert_called_once()
        call_kwargs = backend.stream_range.call_args
        assert call_kwargs.kwargs.get("chunk_size") == 4096

    def test_passes_start_and_end_to_backend(self, stub_fs):
        list(stub_fs.stream_range("/test/file.txt", 10, 50))
        backend = stub_fs.router.route.return_value.backend
        args = backend.stream_range.call_args
        assert args.args[1] == 10  # start
        assert args.args[2] == 50  # end
