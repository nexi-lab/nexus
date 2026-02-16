"""Concurrent stress tests for CASBlobStore (Issue #925).

Verifies lock-free CAS correctness under high contention.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from nexus.backends.cas_blob_store import CASBlobStore
from nexus.core.hash_fast import hash_content

NUM_THREADS = 50


@pytest.fixture
def store(tmp_path):
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    return CASBlobStore(cas_root)


class TestConcurrentSameHash:
    """50 threads writing the same content simultaneously."""

    def test_concurrent_same_hash_writes(self, store):
        content = b"shared content for all threads"
        h = hash_content(content)

        def writer(_i: int) -> str:
            store.store(h, content)
            return h

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            results = [f.result() for f in as_completed(futures)]

        # All threads should return the same hash
        assert all(r == h for r in results)

        # Blob must exist
        assert store.blob_exists(h)
        assert store.read_blob(h) == content

        # ref_count should be exactly NUM_THREADS
        meta = store.read_meta(h)
        assert meta.ref_count == NUM_THREADS

    def test_concurrent_same_hash_ref_count_accuracy(self, store):
        """Write N times, release N-1 times, verify ref_count=1."""
        content = b"ref count accuracy test"
        h = hash_content(content)

        # Phase 1: concurrent stores
        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(store.store, h, content) for _ in range(NUM_THREADS)]
            for f in as_completed(futures):
                f.result()

        assert store.read_meta(h).ref_count == NUM_THREADS

        # Phase 2: concurrent releases (N-1 to leave one ref)
        release_count = NUM_THREADS - 1

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(store.release, h) for _ in range(release_count)]
            for f in as_completed(futures):
                f.result()

        # Blob should still exist with ref_count == 1
        assert store.blob_exists(h)
        meta = store.read_meta(h)
        assert meta.ref_count == 1


class TestConcurrentDifferentHash:
    """50 threads writing different content simultaneously."""

    def test_concurrent_different_hash_writes(self, store):
        def writer(i: int) -> str:
            content = f"unique content {i}".encode()
            h = hash_content(content)
            store.store(h, content)
            return h

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(writer, i) for i in range(NUM_THREADS)]
            hashes = [f.result() for f in as_completed(futures)]

        # All hashes should be unique (different content)
        assert len(set(hashes)) == NUM_THREADS

        # Every blob should exist with ref_count=1
        for h in hashes:
            assert store.blob_exists(h)
            meta = store.read_meta(h)
            assert meta.ref_count == 1


class TestConcurrentStoreRelease:
    """Concurrent store + release stress test."""

    def test_concurrent_store_and_release(self, store):
        """Half threads store, half release — no crashes."""
        content = b"store-release stress"
        h = hash_content(content)

        # Pre-populate so release doesn't hit non-existent
        for _ in range(NUM_THREADS):
            store.store(h, content)
        assert store.read_meta(h).ref_count == NUM_THREADS

        # Half store, half release
        def worker(i: int) -> str:
            if i % 2 == 0:
                store.store(h, content)
                return "store"
            else:
                store.release(h)
                return "release"

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(worker, i) for i in range(NUM_THREADS)]
            for f in as_completed(futures):
                f.result()

        # 50 initial + 25 stores - 25 releases = 50
        meta = store.read_meta(h)
        assert meta.ref_count == NUM_THREADS


class TestIdempotentDoubleWrite:
    """Verify that bypassing exists check still produces correct results."""

    def test_double_write_same_content(self, store):
        """Two writes of identical content produce one blob."""
        content = b"idempotent double write"
        h = hash_content(content)

        store.store(h, content)
        store.store(h, content)

        assert store.read_blob(h) == content
        assert store.read_meta(h).ref_count == 2

    def test_many_concurrent_first_writes(self, store):
        """All threads try to be the first writer — only one blob created."""
        content = b"first writer race"
        h = hash_content(content)

        with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
            futures = [pool.submit(store.write_blob, h, content) for _ in range(NUM_THREADS)]
            for f in as_completed(futures):
                f.result()

        # Exactly one thread should have created the blob
        # (others see it exists and return False)
        # Due to race between exists-check and replace, more than one might
        # "succeed" but the blob content is always correct
        assert store.read_blob(h) == content
        assert store.blob_exists(h)
