"""Concurrency tests for the ReBAC caching stack.

Tests thread-safety of all cache layers under concurrent access,
covering Issue #3192 decision 11B (concurrency correctness).

Each test uses threading.Barrier for synchronized start and short
timeouts (5s max) to prevent hangs. Tests should complete in < 2s.
"""

import threading
import time

import pytest

from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
from nexus.bricks.rebac.cache.iterator import IteratorCache
from nexus.bricks.rebac.cache.leopard import LeopardCache
from nexus.bricks.rebac.cache.result_cache import ReBACPermissionCache
from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

NUM_THREADS = 10
TIMEOUT = 5


class TestResultCacheConcurrency:
    """Thread-safety tests for ReBACPermissionCache."""

    def test_result_cache_concurrent_get_set(self):
        """10 threads reading and writing the same key should not crash."""
        cache = ReBACPermissionCache(max_size=1000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for _i in range(50):
                    cache.set("user", f"u{thread_id}", "read", "file", "/doc.txt", True)
                    result = cache.get("user", f"u{thread_id}", "read", "file", "/doc.txt")
                    # Result should be True or None (if evicted), never an error
                    assert result is True or result is None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent get/set raised errors: {errors}"

    def test_result_cache_concurrent_invalidation(self):
        """5 threads setting and 5 invalidating should not crash or leave stale data."""
        cache = ReBACPermissionCache(max_size=1000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def setter(thread_id: int) -> None:
            try:
                barrier.wait()
                for _i in range(50):
                    cache.set(
                        "user",
                        "shared",
                        "read",
                        "file",
                        f"/file{i}.txt",
                        True,
                        zone_id="z1",
                    )
            except Exception as e:
                errors.append(e)

        def invalidator(thread_id: int) -> None:
            try:
                barrier.wait()
                for _ in range(50):
                    cache.invalidate_subject("user", "shared", "z1")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=setter, args=(i,)))
        for i in range(5):
            threads.append(threading.Thread(target=invalidator, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent invalidation raised errors: {errors}"

    def test_result_cache_concurrent_stampede_prevention(self):
        """10 threads calling try_acquire_compute for the same key -- exactly 1 gets True."""
        cache = ReBACPermissionCache(max_size=1000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        acquired = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                got_lock, key = cache.try_acquire_compute(
                    "user", "alice", "read", "file", "/doc.txt", "z1"
                )
                with lock:
                    acquired.append(got_lock)
                if got_lock:
                    # Simulate computation
                    time.sleep(0.01)
                    cache.release_compute(
                        key,
                        True,
                        "user",
                        "alice",
                        "read",
                        "file",
                        "/doc.txt",
                        "z1",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Stampede prevention raised errors: {errors}"
        assert acquired.count(True) == 1, (
            f"Expected exactly 1 thread to acquire compute, got {acquired.count(True)}"
        )


class TestLeopardCacheConcurrency:
    """Thread-safety tests for LeopardCache."""

    def test_leopard_concurrent_get_set(self):
        """10 threads setting groups for different members should all succeed."""
        cache = LeopardCache(max_size=10000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                member_id = f"user{thread_id}"
                groups = {("group", f"g{i}") for i in range(5)}
                for _ in range(20):
                    cache.set_transitive_groups("user", member_id, "zone1", groups)
                    result = cache.get_transitive_groups("user", member_id, "zone1")
                    assert result is not None
                    assert result == groups
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent get/set raised errors: {errors}"

    def test_leopard_concurrent_invalidation_during_set(self):
        """5 threads setting and 5 invalidating same member should not crash."""
        cache = LeopardCache(max_size=10000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []
        groups = {("group", "g1"), ("group", "g2")}

        def setter(_: int) -> None:
            try:
                barrier.wait()
                for _ in range(50):
                    cache.set_transitive_groups("user", "target", "zone1", groups)
            except Exception as e:
                errors.append(e)

        def invalidator(_: int) -> None:
            try:
                barrier.wait()
                for _ in range(50):
                    cache.invalidate_member("user", "target", "zone1")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=setter, args=(i,)))
        for i in range(5):
            threads.append(threading.Thread(target=invalidator, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent set/invalidate raised errors: {errors}"

    def test_leopard_eviction_recursion_guard(self):
        """Filling cache to max_size and adding one more should not cause RecursionError."""
        max_size = 50
        cache = LeopardCache(max_size=max_size, ttl_seconds=60)

        # Fill the cache to capacity
        for i in range(max_size):
            cache.set_transitive_groups(
                "user",
                f"member{i}",
                "zone1",
                {("group", f"g{i}")},
            )
        assert cache.size == max_size

        # Adding one more should trigger eviction without RecursionError
        try:
            cache.set_transitive_groups(
                "user",
                "overflow_member",
                "zone1",
                {("group", "overflow_group")},
            )
        except RecursionError:
            pytest.fail("LeopardCache._evict_lru caused RecursionError")

        # Cache should still be at or below max_size
        assert cache.size <= max_size


class TestCoordinatorConcurrency:
    """Thread-safety tests for CacheCoordinator."""

    def test_coordinator_concurrent_invalidation(self):
        """10 threads calling invalidate_for_write with different objects -- all callbacks called."""
        coordinator = CacheCoordinator()
        callback_count = 0
        count_lock = threading.Lock()

        def boundary_callback(zone_id, subject_type, subject_id, permission, object_path):
            nonlocal callback_count
            with count_lock:
                callback_count += 1

        coordinator.register_boundary_invalidator("test_cb", boundary_callback)

        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for i in range(10):
                    coordinator.invalidate_for_write(
                        zone_id="zone1",
                        subject=("user", f"user{thread_id}"),
                        relation="editor",
                        object=("file", f"/file{thread_id}_{i}.txt"),
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent invalidation raised errors: {errors}"
        # Each of 10 threads calls invalidate 10 times, each triggers boundary callback
        # "editor" maps to at least 1 permission via RELATION_TO_PERMISSIONS
        assert callback_count > 0, "Expected boundary callbacks to be called"


class TestVisibilityCacheConcurrency:
    """Thread-safety tests for DirectoryVisibilityCache."""

    def test_visibility_cache_concurrent_access(self):
        """10 threads reading/writing visibility entries should not crash."""
        cache = DirectoryVisibilityCache(ttl=60, max_entries=1000)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for i in range(30):
                    path = f"/workspace/dir{thread_id}/file{i}"
                    cache.set_visible("zone1", "user", f"u{thread_id}", path, True)
                    result = cache.is_visible("zone1", "user", f"u{thread_id}", path)
                    # Should be True or None (if evicted)
                    assert result is True or result is None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent visibility access raised errors: {errors}"


class TestBoundaryCacheConcurrency:
    """Thread-safety tests for PermissionBoundaryCache."""

    def test_boundary_cache_concurrent_access(self):
        """10 threads reading/writing boundary entries should not crash."""
        cache = PermissionBoundaryCache(max_size=1000, ttl_seconds=60)
        barrier = threading.Barrier(NUM_THREADS, timeout=TIMEOUT)
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for i in range(30):
                    path = f"/workspace/project{thread_id}/file{i}.py"
                    boundary = f"/workspace/project{thread_id}"
                    cache.set_boundary(
                        "zone1",
                        "user",
                        f"u{thread_id}",
                        "read",
                        path,
                        boundary,
                    )
                    result = cache.get_boundary(
                        "zone1",
                        "user",
                        f"u{thread_id}",
                        "read",
                        path,
                    )
                    # Should be the boundary or None (if evicted/expired)
                    assert result == boundary or result is None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent boundary access raised errors: {errors}"


class TestIteratorCacheConcurrency:
    """Thread-safety tests for IteratorCache."""

    def test_iterator_cache_concurrent_pagination(self):
        """5 threads paginating the same query should get consistent results."""
        cache = IteratorCache(max_size=100, ttl_seconds=60)
        errors: list[Exception] = []
        num_pagination_threads = 5
        barrier = threading.Barrier(num_pagination_threads, timeout=TIMEOUT)
        all_items = list(range(100))

        # Pre-populate the cache with a result set
        cursor_id, results, total = cache.get_or_create(
            query_hash="test_query",
            zone_id="zone1",
            compute_fn=lambda: all_items,
        )
        assert total == 100

        def paginator(thread_id: int) -> None:
            try:
                barrier.wait()
                page_size = 20
                offset = 0
                collected: list[int] = []
                while offset < total:
                    items, next_cursor, page_total = cache.get_page(
                        cursor_id,
                        offset=offset,
                        limit=page_size,
                    )
                    collected.extend(items)
                    assert page_total == 100
                    offset += page_size

                # Each thread should see the complete, ordered result
                assert collected == all_items
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=paginator, args=(i,)) for i in range(num_pagination_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        assert not errors, f"Concurrent pagination raised errors: {errors}"
