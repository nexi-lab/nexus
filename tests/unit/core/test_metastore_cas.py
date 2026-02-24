"""Unit tests for MetastoreABC CAS (compare-and-swap) operations.

Tests the CasResult dataclass, ABC default fallback, and
InMemoryMetastore atomic CAS semantics.
"""

import threading

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import CasResult
from nexus.storage.in_memory_metastore import InMemoryMetastore


def _make_metadata(path: str, version: int = 1) -> FileMetadata:
    """Create a minimal FileMetadata for testing."""
    return FileMetadata(
        path=path,
        backend_name="test",
        physical_path="hash123",
        size=100,
        version=version,
        etag=f"etag-v{version}",
    )


class TestCasResult:
    """Test the CasResult dataclass."""

    def test_cas_result_success(self) -> None:
        r = CasResult(success=True, current_version=2)
        assert r.success is True
        assert r.current_version == 2

    def test_cas_result_failure(self) -> None:
        r = CasResult(success=False, current_version=5)
        assert r.success is False
        assert r.current_version == 5

    def test_cas_result_is_frozen(self) -> None:
        import dataclasses

        r = CasResult(success=True, current_version=1)
        assert dataclasses.is_dataclass(r)
        # Frozen + slots: can create new instances via replace but not mutate
        r2 = dataclasses.replace(r, success=False)
        assert r.success is True  # original unchanged
        assert r2.success is False

    def test_cas_result_equality(self) -> None:
        a = CasResult(success=True, current_version=3)
        b = CasResult(success=True, current_version=3)
        assert a == b

        c = CasResult(success=False, current_version=3)
        assert a != c


class TestAbcDefaultFallback:
    """Test the MetastoreABC.put_if_version() default implementation."""

    def test_abc_default_fallback_success(self) -> None:
        store = InMemoryMetastore()
        meta_v1 = _make_metadata("/test/file.txt", version=1)
        store.put(meta_v1)

        # CAS with correct expected_version → success
        meta_v2 = _make_metadata("/test/file.txt", version=2)
        result = store.put_if_version(meta_v2, expected_version=1)
        assert result.success is True
        assert result.current_version == 2

        # Verify the new metadata was stored
        stored = store.get("/test/file.txt")
        assert stored is not None
        assert stored.version == 2

    def test_abc_default_fallback_mismatch(self) -> None:
        store = InMemoryMetastore()
        meta_v1 = _make_metadata("/test/file.txt", version=1)
        store.put(meta_v1)

        # CAS with wrong expected_version → failure
        meta_v2 = _make_metadata("/test/file.txt", version=2)
        result = store.put_if_version(meta_v2, expected_version=5)
        assert result.success is False
        assert result.current_version == 1

        # Verify the metadata was NOT changed
        stored = store.get("/test/file.txt")
        assert stored is not None
        assert stored.version == 1

    def test_abc_default_create_new(self) -> None:
        store = InMemoryMetastore()

        # CAS create: expected_version=0 for non-existent file
        meta = _make_metadata("/test/new.txt", version=1)
        result = store.put_if_version(meta, expected_version=0)
        assert result.success is True
        assert result.current_version == 1

        # Verify metadata was stored
        stored = store.get("/test/new.txt")
        assert stored is not None
        assert stored.version == 1

    def test_abc_default_create_exists(self) -> None:
        store = InMemoryMetastore()
        meta_v1 = _make_metadata("/test/exists.txt", version=1)
        store.put(meta_v1)

        # CAS create (expected_version=0) when file already exists → failure
        meta_new = _make_metadata("/test/exists.txt", version=1)
        result = store.put_if_version(meta_new, expected_version=0)
        assert result.success is False
        assert result.current_version == 1


class TestInMemoryCas:
    """Test InMemoryMetastore.put_if_version() with atomic semantics."""

    def test_inmemory_cas_success(self) -> None:
        store = InMemoryMetastore()
        meta_v1 = _make_metadata("/file.txt", version=1)
        store.put(meta_v1)

        meta_v2 = _make_metadata("/file.txt", version=2)
        result = store.put_if_version(meta_v2, expected_version=1)
        assert result == CasResult(success=True, current_version=2)

    def test_inmemory_cas_mismatch(self) -> None:
        store = InMemoryMetastore()
        meta_v3 = _make_metadata("/file.txt", version=3)
        store.put(meta_v3)

        meta_v4 = _make_metadata("/file.txt", version=4)
        result = store.put_if_version(meta_v4, expected_version=1)
        assert result == CasResult(success=False, current_version=3)

    def test_inmemory_cas_concurrent_single_winner(self) -> None:
        """Verify that under concurrent CAS, exactly one writer wins per round."""
        store = InMemoryMetastore()
        meta_v1 = _make_metadata("/race.txt", version=1)
        store.put(meta_v1)

        num_threads = 20
        results: list[CasResult] = [CasResult(success=False, current_version=0)] * num_threads

        def cas_writer(idx: int) -> None:
            meta = _make_metadata("/race.txt", version=2)
            results[idx] = store.put_if_version(meta, expected_version=1)

        threads = [threading.Thread(target=cas_writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should have succeeded
        success_count = sum(1 for r in results if r.success)
        assert success_count == 1, f"Expected exactly 1 winner, got {success_count}"

        # The stored version should be 2
        stored = store.get("/race.txt")
        assert stored is not None
        assert stored.version == 2

    def test_put_signature_compatibility(self) -> None:
        """Verify put() accepts consistency kwarg (ABC signature fix)."""
        store = InMemoryMetastore()
        meta = _make_metadata("/file.txt", version=1)
        result = store.put(meta, consistency="ec")
        assert result is None
        assert store.get("/file.txt") is not None
