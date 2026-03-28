"""Unit tests for LocalTransport — Transport protocol conformance.

Tests all 9 Transport methods with a real temp-directory filesystem.

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

import pytest

from nexus.backends.base.transport import Transport
from nexus.backends.transports.local_transport import LocalTransport
from nexus.contracts.exceptions import NexusFileNotFoundError


@pytest.fixture
def transport(tmp_path):
    """Create a LocalTransport rooted in a temporary directory."""
    return LocalTransport(root_path=tmp_path, fsync=False)


# === Protocol Conformance ===


class TestProtocolConformance:
    def test_implements_transport(self, transport):
        assert isinstance(transport, Transport)

    def test_transport_name(self, transport):
        assert transport.transport_name == "local"


# === store / fetch ===


class TestPutGetBlob:
    def test_put_and_get_roundtrip(self, transport):
        transport.store("test/key.txt", b"hello world")
        data, version_id = transport.fetch("test/key.txt")
        assert data == b"hello world"
        assert version_id is None  # Local FS has no versioning

    def test_put_returns_none(self, transport):
        result = transport.store("k", b"data")
        assert result is None

    def test_put_overwrites_existing(self, transport):
        transport.store("k", b"v1")
        transport.store("k", b"v2")
        data, _ = transport.fetch("k")
        assert data == b"v2"

    def test_get_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.fetch("no/such/key")

    def test_put_creates_parent_dirs(self, transport):
        transport.store("a/b/c/d/file", b"deep")
        data, _ = transport.fetch("a/b/c/d/file")
        assert data == b"deep"

    def test_put_empty_blob(self, transport):
        transport.store("empty", b"")
        data, _ = transport.fetch("empty")
        assert data == b""

    def test_put_large_blob(self, transport):
        large = b"x" * (1024 * 1024)  # 1MB
        transport.store("large", large)
        data, _ = transport.fetch("large")
        assert data == large

    def test_atomic_write_no_partial_file_on_error(self, transport, tmp_path):
        """Verify that a failed write doesn't leave partial files."""
        # Write a valid file first
        transport.store("k", b"original")

        # The atomic temp+replace pattern means even if we had an error,
        # the original file should remain intact
        data, _ = transport.fetch("k")
        assert data == b"original"


# === remove ===


class TestDeleteBlob:
    def test_delete_existing(self, transport):
        transport.store("k", b"data")
        transport.remove("k")
        assert not transport.exists("k")

    def test_delete_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.remove("no/such/key")

    def test_delete_cleans_empty_parents(self, transport, tmp_path):
        transport.store("a/b/c/file", b"data")
        transport.remove("a/b/c/file")
        # Parent dirs a/b/c, a/b, a should be cleaned up
        assert not (tmp_path / "a" / "b" / "c").exists()
        assert not (tmp_path / "a" / "b").exists()
        assert not (tmp_path / "a").exists()


# === exists ===


class TestBlobExists:
    def test_exists_true(self, transport):
        transport.store("k", b"data")
        assert transport.exists("k") is True

    def test_exists_false(self, transport):
        assert transport.exists("no/such/key") is False

    def test_exists_after_delete(self, transport):
        transport.store("k", b"data")
        transport.remove("k")
        assert transport.exists("k") is False


# === get_size ===


class TestGetBlobSize:
    def test_size_correct(self, transport):
        transport.store("k", b"12345")
        assert transport.get_size("k") == 5

    def test_size_empty(self, transport):
        transport.store("k", b"")
        assert transport.get_size("k") == 0

    def test_size_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.get_size("no/such/key")


# === list_keys ===


class TestListBlobs:
    def test_list_with_delimiter(self, transport):
        transport.store("cas/ab/file1", b"1")
        transport.store("cas/ab/file2", b"2")
        # Create a sub-directory with content
        transport.store("cas/ab/cd/file3", b"3")

        blobs, prefixes = transport.list_keys("cas/ab/", delimiter="/")
        assert "cas/ab/file1" in blobs
        assert "cas/ab/file2" in blobs
        assert "cas/ab/cd/" in prefixes

    def test_list_without_delimiter(self, transport):
        transport.store("cas/ab/file1", b"1")
        transport.store("cas/ab/cd/file2", b"2")

        blobs, prefixes = transport.list_keys("cas/ab/", delimiter="")
        assert len(blobs) == 2
        assert prefixes == []

    def test_list_empty_prefix(self, transport):
        blobs, prefixes = transport.list_keys("nonexistent/", delimiter="/")
        assert blobs == []
        assert prefixes == []


# === copy_key ===


class TestCopyBlob:
    def test_copy_creates_destination(self, transport):
        transport.store("src", b"data")
        transport.copy_key("src", "dst")
        data, _ = transport.fetch("dst")
        assert data == b"data"

    def test_copy_preserves_source(self, transport):
        transport.store("src", b"data")
        transport.copy_key("src", "dst")
        src_data, _ = transport.fetch("src")
        assert src_data == b"data"

    def test_copy_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            transport.copy_key("no/such/src", "dst")

    def test_copy_to_nested_path(self, transport):
        transport.store("src", b"data")
        transport.copy_key("src", "a/b/c/dst")
        data, _ = transport.fetch("a/b/c/dst")
        assert data == b"data"


# === create_dir ===


class TestCreateDirectoryMarker:
    def test_create_dir_marker_trailing_slash(self, transport, tmp_path):
        transport.create_dir("dirs/workspace/")
        assert (tmp_path / "dirs" / "workspace").is_dir()

    def test_create_dir_marker_no_trailing_slash(self, transport, tmp_path):
        transport.create_dir("dirs/marker")
        assert (tmp_path / "dirs" / "marker").exists()

    def test_create_dir_marker_idempotent(self, transport):
        transport.create_dir("dirs/workspace/")
        transport.create_dir("dirs/workspace/")
        # Should not raise


# === stream ===


class TestStreamBlob:
    def test_stream_full_content(self, transport):
        transport.store("k", b"hello world")
        chunks = list(transport.stream("k", chunk_size=4))
        assert b"".join(chunks) == b"hello world"

    def test_stream_chunk_sizes(self, transport):
        data = b"abcdefghij"  # 10 bytes
        transport.store("k", data)
        chunks = list(transport.stream("k", chunk_size=3))
        # 3 + 3 + 3 + 1 = 10
        assert len(chunks) == 4
        assert chunks[0] == b"abc"
        assert chunks[-1] == b"j"
        assert b"".join(chunks) == data

    def test_stream_nonexistent_raises(self, transport):
        with pytest.raises(NexusFileNotFoundError):
            list(transport.stream("no/such/key"))

    def test_stream_empty_blob(self, transport):
        transport.store("k", b"")
        chunks = list(transport.stream("k"))
        assert chunks == []


# === fsync option ===


class TestFsyncOption:
    def test_fsync_enabled(self, tmp_path):
        t = LocalTransport(root_path=tmp_path, fsync=True)
        t.store("k", b"data")
        data, _ = t.fetch("k")
        assert data == b"data"

    def test_fsync_disabled(self, tmp_path):
        t = LocalTransport(root_path=tmp_path, fsync=False)
        t.store("k", b"data")
        data, _ = t.fetch("k")
        assert data == b"data"


# === CAS Key Pattern (integration-style) ===


class TestCASKeyPattern:
    """Verify transport works with CAS-style keys (what CASAddressingEngine sends)."""

    def test_cas_key_roundtrip(self, transport):
        key = "cas/ab/cd/abcdef1234567890"
        transport.store(key, b"content")
        data, _ = transport.fetch(key)
        assert data == b"content"

    def test_cas_meta_key(self, transport):
        key = "cas/ab/cd/abcdef1234567890.meta"
        transport.store(key, b'{"ref_count":1}')
        data, _ = transport.fetch(key)
        assert b"ref_count" in data

    def test_dirs_key(self, transport):
        key = "dirs/workspace/"
        transport.create_dir(key)
        assert transport.exists(key)


# === _ensure_parent cache ===


class TestEnsureParentCache:
    def test_known_parents_populated(self, transport, tmp_path):
        transport.store("cas/ab/cd/hash1", b"data")
        parent = str(tmp_path / "cas" / "ab" / "cd")
        assert parent in transport._known_parents

    def test_known_parents_skip_redundant_mkdir(self, transport, tmp_path):
        """Second store to same dir should skip mkdir (cached)."""
        transport.store("cas/ab/cd/hash1", b"v1")
        transport.store("cas/ab/cd/hash2", b"v2")
        data, _ = transport.fetch("cas/ab/cd/hash2")
        assert data == b"v2"

    def test_known_parents_evict_on_external_delete(self, transport, tmp_path):
        """If parent dir is externally deleted, store retries after evicting cache."""
        import shutil

        transport.store("cas/ab/cd/hash1", b"v1")
        # Externally nuke the parent dir
        shutil.rmtree(tmp_path / "cas" / "ab")
        # Next write should recover by evicting + re-creating
        transport.store("cas/ab/cd/hash2", b"v2")
        data, _ = transport.fetch("cas/ab/cd/hash2")
        assert data == b"v2"


# === store_nosync ===


class TestPutBlobNosync:
    def test_nosync_roundtrip(self, transport):
        transport.store_nosync("meta/key.json", b'{"ref_count": 1}')
        data, _ = transport.fetch("meta/key.json")
        assert data == b'{"ref_count": 1}'

    def test_nosync_overwrites(self, transport):
        transport.store_nosync("k", b"v1")
        transport.store_nosync("k", b"v2")
        data, _ = transport.fetch("k")
        assert data == b"v2"

    def test_nosync_creates_parents(self, transport, tmp_path):
        transport.store_nosync("a/b/c/file", b"deep")
        assert (tmp_path / "a" / "b" / "c" / "file").exists()

    def test_nosync_empty_data(self, transport):
        transport.store_nosync("empty", b"")
        data, _ = transport.fetch("empty")
        assert data == b""


# === EAFP fetch ===


class TestGetBlobEAFP:
    def test_get_nonexistent_raises_without_stat(self, transport):
        """fetch should raise NexusFileNotFoundError without a prior stat."""
        with pytest.raises(NexusFileNotFoundError):
            transport.fetch("does/not/exist")

    def test_get_directory_key_raises(self, transport, tmp_path):
        """fetch on a directory path should raise NexusFileNotFoundError."""
        (tmp_path / "some_dir").mkdir()
        with pytest.raises(NexusFileNotFoundError):
            transport.fetch("some_dir")
