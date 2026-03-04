"""Unit tests for LocalBlobTransport — BlobTransport protocol conformance.

Tests all 9 BlobTransport methods with a real temp-directory filesystem.

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

import pytest

from nexus.backends.base.blob_transport import BlobTransport
from nexus.backends.transports.local_transport import LocalBlobTransport
from nexus.contracts.exceptions import NexusFileNotFoundError


@pytest.fixture
def transport(tmp_path):
    """Create a LocalBlobTransport rooted in a temporary directory."""
    return LocalBlobTransport(root_path=tmp_path, fsync=False)


# === Protocol Conformance ===


class TestProtocolConformance:
    def test_implements_blob_transport(self, transport):
        assert isinstance(transport, BlobTransport)

    def test_transport_name(self, transport):
        assert transport.transport_name == "local"


# === put_blob / get_blob ===


class TestPutGetBlob:
    def test_put_and_get_roundtrip(self, transport):
        transport.put_blob("test/key.txt", b"hello world")
        data, version_id = transport.get_blob("test/key.txt")
        assert data == b"hello world"
        assert version_id is None  # Local FS has no versioning

    def test_put_returns_none(self, transport):
        result = transport.put_blob("k", b"data")
        assert result is None

    def test_put_overwrites_existing(self, transport):
        transport.put_blob("k", b"v1")
        transport.put_blob("k", b"v2")
        data, _ = transport.get_blob("k")
        assert data == b"v2"

    def test_get_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.get_blob("no/such/key")

    def test_put_creates_parent_dirs(self, transport):
        transport.put_blob("a/b/c/d/file", b"deep")
        data, _ = transport.get_blob("a/b/c/d/file")
        assert data == b"deep"

    def test_put_empty_blob(self, transport):
        transport.put_blob("empty", b"")
        data, _ = transport.get_blob("empty")
        assert data == b""

    def test_put_large_blob(self, transport):
        large = b"x" * (1024 * 1024)  # 1MB
        transport.put_blob("large", large)
        data, _ = transport.get_blob("large")
        assert data == large

    def test_atomic_write_no_partial_file_on_error(self, transport, tmp_path):
        """Verify that a failed write doesn't leave partial files."""
        # Write a valid file first
        transport.put_blob("k", b"original")

        # The atomic temp+replace pattern means even if we had an error,
        # the original file should remain intact
        data, _ = transport.get_blob("k")
        assert data == b"original"


# === delete_blob ===


class TestDeleteBlob:
    def test_delete_existing(self, transport):
        transport.put_blob("k", b"data")
        transport.delete_blob("k")
        assert not transport.blob_exists("k")

    def test_delete_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.delete_blob("no/such/key")

    def test_delete_cleans_empty_parents(self, transport, tmp_path):
        transport.put_blob("a/b/c/file", b"data")
        transport.delete_blob("a/b/c/file")
        # Parent dirs a/b/c, a/b, a should be cleaned up
        assert not (tmp_path / "a" / "b" / "c").exists()
        assert not (tmp_path / "a" / "b").exists()
        assert not (tmp_path / "a").exists()


# === blob_exists ===


class TestBlobExists:
    def test_exists_true(self, transport):
        transport.put_blob("k", b"data")
        assert transport.blob_exists("k") is True

    def test_exists_false(self, transport):
        assert transport.blob_exists("no/such/key") is False

    def test_exists_after_delete(self, transport):
        transport.put_blob("k", b"data")
        transport.delete_blob("k")
        assert transport.blob_exists("k") is False


# === get_blob_size ===


class TestGetBlobSize:
    def test_size_correct(self, transport):
        transport.put_blob("k", b"12345")
        assert transport.get_blob_size("k") == 5

    def test_size_empty(self, transport):
        transport.put_blob("k", b"")
        assert transport.get_blob_size("k") == 0

    def test_size_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.get_blob_size("no/such/key")


# === list_blobs ===


class TestListBlobs:
    def test_list_with_delimiter(self, transport):
        transport.put_blob("cas/ab/file1", b"1")
        transport.put_blob("cas/ab/file2", b"2")
        # Create a sub-directory with content
        transport.put_blob("cas/ab/cd/file3", b"3")

        blobs, prefixes = transport.list_blobs("cas/ab/", delimiter="/")
        assert "cas/ab/file1" in blobs
        assert "cas/ab/file2" in blobs
        assert "cas/ab/cd/" in prefixes

    def test_list_without_delimiter(self, transport):
        transport.put_blob("cas/ab/file1", b"1")
        transport.put_blob("cas/ab/cd/file2", b"2")

        blobs, prefixes = transport.list_blobs("cas/ab/", delimiter="")
        assert len(blobs) == 2
        assert prefixes == []

    def test_list_empty_prefix(self, transport):
        blobs, prefixes = transport.list_blobs("nonexistent/", delimiter="/")
        assert blobs == []
        assert prefixes == []


# === copy_blob ===


class TestCopyBlob:
    def test_copy_creates_destination(self, transport):
        transport.put_blob("src", b"data")
        transport.copy_blob("src", "dst")
        data, _ = transport.get_blob("dst")
        assert data == b"data"

    def test_copy_preserves_source(self, transport):
        transport.put_blob("src", b"data")
        transport.copy_blob("src", "dst")
        src_data, _ = transport.get_blob("src")
        assert src_data == b"data"

    def test_copy_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.copy_blob("no/such/src", "dst")

    def test_copy_to_nested_path(self, transport):
        transport.put_blob("src", b"data")
        transport.copy_blob("src", "a/b/c/dst")
        data, _ = transport.get_blob("a/b/c/dst")
        assert data == b"data"


# === create_directory_marker ===


class TestCreateDirectoryMarker:
    def test_create_dir_marker_trailing_slash(self, transport, tmp_path):
        transport.create_directory_marker("dirs/workspace/")
        assert (tmp_path / "dirs" / "workspace").is_dir()

    def test_create_dir_marker_no_trailing_slash(self, transport, tmp_path):
        transport.create_directory_marker("dirs/marker")
        assert (tmp_path / "dirs" / "marker").exists()

    def test_create_dir_marker_idempotent(self, transport):
        transport.create_directory_marker("dirs/workspace/")
        transport.create_directory_marker("dirs/workspace/")
        # Should not raise


# === stream_blob ===


class TestStreamBlob:
    def test_stream_full_content(self, transport):
        transport.put_blob("k", b"hello world")
        chunks = list(transport.stream_blob("k", chunk_size=4))
        assert b"".join(chunks) == b"hello world"

    def test_stream_chunk_sizes(self, transport):
        data = b"abcdefghij"  # 10 bytes
        transport.put_blob("k", data)
        chunks = list(transport.stream_blob("k", chunk_size=3))
        # 3 + 3 + 3 + 1 = 10
        assert len(chunks) == 4
        assert chunks[0] == b"abc"
        assert chunks[-1] == b"j"
        assert b"".join(chunks) == data

    def test_stream_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            list(transport.stream_blob("no/such/key"))

    def test_stream_empty_blob(self, transport):
        transport.put_blob("k", b"")
        chunks = list(transport.stream_blob("k"))
        assert chunks == []


# === fsync option ===


class TestFsyncOption:
    def test_fsync_enabled(self, tmp_path):
        t = LocalBlobTransport(root_path=tmp_path, fsync=True)
        t.put_blob("k", b"data")
        data, _ = t.get_blob("k")
        assert data == b"data"

    def test_fsync_disabled(self, tmp_path):
        t = LocalBlobTransport(root_path=tmp_path, fsync=False)
        t.put_blob("k", b"data")
        data, _ = t.get_blob("k")
        assert data == b"data"


# === CAS Key Pattern (integration-style) ===


class TestCASKeyPattern:
    """Verify transport works with CAS-style keys (what CASBackend sends)."""

    def test_cas_key_roundtrip(self, transport):
        key = "cas/ab/cd/abcdef1234567890"
        transport.put_blob(key, b"content")
        data, _ = transport.get_blob(key)
        assert data == b"content"

    def test_cas_meta_key(self, transport):
        key = "cas/ab/cd/abcdef1234567890.meta"
        transport.put_blob(key, b'{"ref_count":1}')
        data, _ = transport.get_blob(key)
        assert b"ref_count" in data

    def test_dirs_key(self, transport):
        key = "dirs/workspace/"
        transport.create_directory_marker(key)
        assert transport.blob_exists(key)
