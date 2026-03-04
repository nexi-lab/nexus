"""Integration tests for ObjectStoreABC — Backend directly implements the ABC.

Validates that Backend (which extends ObjectStoreABC) works correctly as the
kernel file_operations contract, matching the CacheStoreABC integration test pattern.

Since BackendObjectStore adapter was removed (Backend now *is* the ObjectStoreABC),
tests exercise LocalBackend directly through the ABC interface.
"""

import pytest

from nexus.backends.storage.local import LocalBackend
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import ObjectStoreABC


class TestBackendAsObjectStore:
    """Tests that Backend directly satisfies ObjectStoreABC contract."""

    def test_local_backend_is_object_store(self, tmp_path) -> None:
        """LocalBackend is an ObjectStoreABC instance."""
        backend = LocalBackend(root_path=str(tmp_path))
        assert isinstance(backend, ObjectStoreABC)
        assert backend.name == "local"

    def test_write_read_roundtrip(self, tmp_path) -> None:
        """Full roundtrip through LocalBackend."""
        backend = LocalBackend(root_path=str(tmp_path))

        content = b"integration test data"
        result = backend.write_content(content)
        assert len(result.content_hash) == 64
        assert backend.read_content(result.content_hash) == content

    def test_all_ops(self, tmp_path) -> None:
        """Exercises core ObjectStoreABC methods through LocalBackend."""
        backend = LocalBackend(root_path=str(tmp_path))

        # write_content
        r1 = backend.write_content(b"first")
        r2 = backend.write_content(b"second")

        # read_content
        assert backend.read_content(r1.content_hash) == b"first"
        assert backend.read_content(r2.content_hash) == b"second"

        # content_exists
        assert backend.content_exists(r1.content_hash) is True
        assert backend.content_exists("f" * 64) is False

        # get_content_size
        assert backend.get_content_size(r1.content_hash) == 5
        assert backend.get_content_size(r2.content_hash) == 6

        # batch_read_content
        result = backend.batch_read_content([r1.content_hash, r2.content_hash, "f" * 64])
        assert result[r1.content_hash] == b"first"
        assert result[r2.content_hash] == b"second"
        assert result["f" * 64] is None

        # delete_content
        backend.delete_content(r1.content_hash)
        assert backend.content_exists(r1.content_hash) is False

    def test_error_propagation(self, tmp_path) -> None:
        """Errors from LocalBackend raise proper exceptions."""
        backend = LocalBackend(root_path=str(tmp_path))

        with pytest.raises((NexusFileNotFoundError, BackendError)):
            backend.read_content("d" * 64)

    def test_deduplication(self, tmp_path) -> None:
        """Deduplication works — same content produces same hash."""
        backend = LocalBackend(root_path=str(tmp_path))

        content = b"deduplicate me"
        r1 = backend.write_content(content)
        r2 = backend.write_content(content)
        assert r1.content_hash == r2.content_hash

        # Delete once — should decrement ref count
        backend.delete_content(r1.content_hash)
        # Content still accessible (ref count > 0)
        assert backend.content_exists(r2.content_hash)
        assert backend.read_content(r2.content_hash) == content
