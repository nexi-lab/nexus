"""Unit tests for passthrough backend."""

import pytest

from nexus.backends.passthrough import PassthroughBackend, POINTER_PREFIX
from nexus.core.hash_fast import hash_content


@pytest.fixture
def temp_backend(tmp_path):
    """Create a temporary passthrough backend for testing."""
    backend = PassthroughBackend(base_path=tmp_path / "passthrough")
    yield backend


class TestInitialization:
    """Tests for backend initialization."""

    def test_creates_required_directories(self, tmp_path):
        """Test backend initialization creates pointers/ and cas/ directories."""
        root = tmp_path / "test_backend"
        backend = PassthroughBackend(root)

        assert backend.base_path == root.resolve()
        assert backend.pointers_root == root / "pointers"
        assert backend.cas_root == root / "cas"
        assert backend.pointers_root.exists()
        assert backend.cas_root.exists()

    def test_backend_name(self, temp_backend):
        """Test that backend name property returns correct value."""
        assert temp_backend.name == "passthrough"


class TestContentOperations:
    """Tests for CAS content operations."""

    def test_write_and_read_content(self, temp_backend):
        """Test writing and reading content."""
        content = b"Hello, World!"
        content_hash = temp_backend.write_content(content).unwrap()

        # Verify hash is correct
        expected_hash = hash_content(content)
        assert content_hash == expected_hash

        # Read content back
        retrieved = temp_backend.read_content(content_hash).unwrap()
        assert retrieved == content

    def test_write_duplicate_content(self, temp_backend):
        """Test writing duplicate content returns same hash."""
        content = b"Duplicate test content"

        hash1 = temp_backend.write_content(content).unwrap()
        hash2 = temp_backend.write_content(content).unwrap()

        assert hash1 == hash2

    def test_read_nonexistent_content(self, temp_backend):
        """Test reading non-existent content returns not_found."""
        fake_hash = "a" * 64
        response = temp_backend.read_content(fake_hash)
        assert not response.success
        assert response.error_code == 404

    def test_content_exists(self, temp_backend):
        """Test checking if content exists."""
        content = b"Existence test"
        content_hash = temp_backend.write_content(content).unwrap()

        assert temp_backend.content_exists(content_hash).unwrap() is True

        fake_hash = "b" * 64
        assert temp_backend.content_exists(fake_hash).unwrap() is False

    def test_get_content_size(self, temp_backend):
        """Test getting content size."""
        content = b"Size test content"
        content_hash = temp_backend.write_content(content).unwrap()

        size = temp_backend.get_content_size(content_hash).unwrap()
        assert size == len(content)

    def test_cas_path_structure(self, temp_backend):
        """Test CAS uses two-level directory structure."""
        content = b"Path structure test"
        content_hash = temp_backend.write_content(content).unwrap()

        cas_path = temp_backend._get_cas_path(content_hash)
        # Should be: cas/ab/cd/abcd...
        assert cas_path.parent.name == content_hash[2:4]
        assert cas_path.parent.parent.name == content_hash[:2]
        assert cas_path.name == content_hash


class TestPointerOperations:
    """Tests for pointer file operations."""

    def test_write_and_read_pointer(self, temp_backend):
        """Test writing and reading pointer files."""
        virtual_path = "/inbox/test.txt"
        content_hash = "abcd1234" + "0" * 56

        temp_backend._write_pointer(virtual_path, content_hash)
        read_hash = temp_backend._read_pointer(virtual_path)

        assert read_hash == content_hash

    def test_pointer_file_format(self, temp_backend):
        """Test pointer file contains correct format."""
        virtual_path = "/inbox/test.txt"
        content_hash = "abcd1234" + "0" * 56

        temp_backend._write_pointer(virtual_path, content_hash)

        pointer_path = temp_backend._get_pointer_path(virtual_path)
        content = pointer_path.read_text()
        assert content == f"{POINTER_PREFIX}{content_hash}\n"

    def test_write_content_with_virtual_path(self, temp_backend):
        """Test writing content creates pointer when virtual_path provided."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        content = b"Content with pointer"
        context = MockContext(virtual_path="/inbox/file.txt")

        content_hash = temp_backend.write_content(content, context=context).unwrap()

        # Verify pointer was created
        pointer_path = temp_backend._get_pointer_path("/inbox/file.txt")
        assert pointer_path.exists()

        # Verify pointer points to correct hash
        read_hash = temp_backend._read_pointer("/inbox/file.txt")
        assert read_hash == content_hash

    def test_delete_pointer(self, temp_backend):
        """Test deleting pointer files."""
        virtual_path = "/inbox/delete_me.txt"
        content_hash = "abcd1234" + "0" * 56

        temp_backend._write_pointer(virtual_path, content_hash)
        assert temp_backend._read_pointer(virtual_path) == content_hash

        deleted = temp_backend._delete_pointer(virtual_path)
        assert deleted is True
        assert temp_backend._read_pointer(virtual_path) is None

    def test_get_physical_path(self, temp_backend):
        """Test get_physical_path returns correct pointer path."""
        physical = temp_backend.get_physical_path("/inbox/test.txt")
        expected = temp_backend.pointers_root / "inbox" / "test.txt"
        assert physical == expected


class TestDirectoryOperations:
    """Tests for directory operations."""

    def test_mkdir(self, temp_backend):
        """Test creating directories."""
        temp_backend.mkdir("/test/nested", parents=True).unwrap()

        dir_path = temp_backend._get_pointer_path("/test/nested")
        assert dir_path.exists()
        assert dir_path.is_dir()

    def test_rmdir(self, temp_backend):
        """Test removing directories."""
        temp_backend.mkdir("/to_delete", parents=True).unwrap()
        temp_backend.rmdir("/to_delete").unwrap()

        dir_path = temp_backend._get_pointer_path("/to_delete")
        assert not dir_path.exists()

    def test_is_directory(self, temp_backend):
        """Test checking if path is directory."""
        temp_backend.mkdir("/is_dir_test", parents=True).unwrap()

        assert temp_backend.is_directory("/is_dir_test").unwrap() is True
        assert temp_backend.is_directory("/nonexistent").unwrap() is False

    def test_list_dir(self, temp_backend):
        """Test listing directory contents."""
        # Create some files and directories
        temp_backend.mkdir("/list_test/subdir", parents=True).unwrap()
        temp_backend._write_pointer("/list_test/file1.txt", "hash1" + "0" * 59)
        temp_backend._write_pointer("/list_test/file2.txt", "hash2" + "0" * 59)

        entries = temp_backend.list_dir("/list_test")

        assert "subdir/" in entries  # Directory has trailing slash
        assert "file1.txt" in entries
        assert "file2.txt" in entries

    def test_list_dir_excludes_tmp_files(self, temp_backend):
        """Test that list_dir excludes .tmp files."""
        temp_backend.mkdir("/tmp_test", parents=True).unwrap()

        # Create a normal file and a temp file
        pointer_path = temp_backend._get_pointer_path("/tmp_test")
        (pointer_path / "normal.txt").write_text("content")
        (pointer_path / "temp.txt.tmp").write_text("temp content")

        entries = temp_backend.list_dir("/tmp_test")

        assert "normal.txt" in entries
        assert "temp.txt.tmp" not in entries


class TestLocking:
    """Tests for advisory locking."""

    def test_lock_and_unlock(self, temp_backend):
        """Test basic lock and unlock."""
        lock_id = temp_backend.lock("/test/file.txt", timeout=1.0)
        assert lock_id is not None

        assert temp_backend.is_locked("/test/file.txt") is True

        released = temp_backend.unlock(lock_id)
        assert released is True

        assert temp_backend.is_locked("/test/file.txt") is False

    def test_lock_prevents_second_lock(self, temp_backend):
        """Test that locked path cannot be locked again."""
        lock_id1 = temp_backend.lock("/exclusive.txt", timeout=1.0)
        assert lock_id1 is not None

        # Second lock should timeout
        lock_id2 = temp_backend.lock("/exclusive.txt", timeout=0.2)
        assert lock_id2 is None

        # Cleanup
        temp_backend.unlock(lock_id1)

    def test_unlock_invalid_lock_id(self, temp_backend):
        """Test unlocking with invalid lock_id returns False."""
        released = temp_backend.unlock("invalid-lock-id")
        assert released is False

    def test_lock_different_paths(self, temp_backend):
        """Test that different paths can be locked independently."""
        lock_id1 = temp_backend.lock("/path1.txt", timeout=1.0)
        lock_id2 = temp_backend.lock("/path2.txt", timeout=1.0)

        assert lock_id1 is not None
        assert lock_id2 is not None
        assert lock_id1 != lock_id2

        # Cleanup
        temp_backend.unlock(lock_id1)
        temp_backend.unlock(lock_id2)


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_content(self, temp_backend):
        """Test writing empty content."""
        content = b""
        content_hash = temp_backend.write_content(content).unwrap()

        expected_hash = hash_content(b"")
        assert content_hash == expected_hash

        retrieved = temp_backend.read_content(content_hash).unwrap()
        assert retrieved == b""

    def test_large_content(self, temp_backend):
        """Test writing large content."""
        content = b"X" * (1024 * 1024)  # 1 MB
        content_hash = temp_backend.write_content(content).unwrap()

        retrieved = temp_backend.read_content(content_hash).unwrap()
        assert len(retrieved) == len(content)
        assert retrieved == content

    def test_invalid_hash_length(self, temp_backend):
        """Test that invalid hash length raises error."""
        with pytest.raises(ValueError, match="Invalid hash length"):
            temp_backend._get_cas_path("abc")

    def test_nested_pointer_path(self, temp_backend):
        """Test deeply nested pointer paths."""
        from dataclasses import dataclass

        @dataclass
        class MockContext:
            virtual_path: str

        content = b"Nested content"
        context = MockContext(virtual_path="/a/b/c/d/e/f/file.txt")

        content_hash = temp_backend.write_content(content, context=context).unwrap()

        # Verify pointer exists
        read_hash = temp_backend._read_pointer("/a/b/c/d/e/f/file.txt")
        assert read_hash == content_hash