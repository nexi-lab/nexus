"""Integration tests for ObjectStoreABC — full stack: factory → adapter → backend.

Validates that BackendObjectStore works correctly when wrapping a backend
created by BackendFactory, matching the CacheStoreABC integration test pattern.
"""

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import BackendObjectStore, ObjectStoreABC

class TestFactoryToAdapterIntegration:
    """Tests the full chain: BackendFactory → Backend → BackendObjectStore."""

    def test_wrap_local_backend(self, tmp_path) -> None:
        """LocalBackend created directly wraps into ObjectStoreABC."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)
        assert isinstance(store, ObjectStoreABC)
        assert store.name == "local"

    def test_write_read_roundtrip_via_factory(self, tmp_path) -> None:
        """Full roundtrip through factory-created backend."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)

        content = b"integration test data"
        content_hash = store.write(content)
        assert len(content_hash) == 64
        assert store.read(content_hash) == content

    def test_all_ops_through_adapter(self, tmp_path) -> None:
        """Exercises all 6 ObjectStoreABC methods through the adapter."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)

        # write
        h1 = store.write(b"first")
        h2 = store.write(b"second")

        # read
        assert store.read(h1) == b"first"
        assert store.read(h2) == b"second"

        # exists
        assert store.exists(h1) is True
        assert store.exists("f" * 64) is False

        # size
        assert store.size(h1) == 5
        assert store.size(h2) == 6

        # batch_read
        result = store.batch_read([h1, h2, "f" * 64])
        assert result[h1] == b"first"
        assert result[h2] == b"second"
        assert result["f" * 64] is None

        # delete
        store.delete(h1)
        assert store.exists(h1) is False

    def test_backend_property_accessible(self, tmp_path) -> None:
        """Read-only backend property works for introspection."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)
        assert store.backend is backend
        assert isinstance(store.backend, LocalBackend)

    def test_repr_includes_backend_name(self, tmp_path) -> None:
        """Repr is useful for debugging."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)
        r = repr(store)
        assert "BackendObjectStore" in r
        assert "local" in r

    def test_error_propagation_through_full_stack(self, tmp_path) -> None:
        """Errors from real LocalBackend propagate through adapter correctly."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)

        with pytest.raises((NexusFileNotFoundError, BackendError)):
            store.read("d" * 64)

    def test_deduplication_through_full_stack(self, tmp_path) -> None:
        """Deduplication works through the full adapter stack."""
        backend = LocalBackend(root_path=str(tmp_path))
        store = BackendObjectStore(backend)

        content = b"deduplicate me"
        h1 = store.write(content)
        h2 = store.write(content)
        assert h1 == h2

        # Delete once — should decrement ref count
        store.delete(h1)
        # Content still accessible (ref count > 0)
        assert store.exists(h2)
        assert store.read(h2) == content
