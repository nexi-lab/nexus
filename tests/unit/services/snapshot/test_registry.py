"""Unit tests for TransactionRegistry (Issue #1752).

Tests: Register, TrackPath, FastPath, Unregister, Concurrent.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from nexus.services.snapshot.registry import TransactionRegistry

class TestRegister:
    """Tests for register()."""

    def test_register_new_transaction(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        assert registry.active_count == 1
        assert registry.has_active_transactions()

    def test_register_idempotent(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.register("txn-1")
        assert registry.active_count == 1

    def test_register_multiple(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.register("txn-2")
        assert registry.active_count == 2

class TestTrackPath:
    """Tests for track_path()."""

    def test_track_path_success(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        assert registry.track_path("txn-1", "/file.txt") is True
        assert registry.get_transaction_for_path("/file.txt") == "txn-1"

    def test_track_path_same_transaction_idempotent(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        assert registry.track_path("txn-1", "/file.txt") is True
        assert registry.track_path("txn-1", "/file.txt") is True  # no conflict with self

    def test_track_path_conflict_different_transaction(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.register("txn-2")
        registry.track_path("txn-1", "/file.txt")
        assert registry.track_path("txn-2", "/file.txt") is False

    def test_track_multiple_paths(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.track_path("txn-1", "/a.txt")
        registry.track_path("txn-1", "/b.txt")
        paths = registry.get_paths("txn-1")
        assert paths == frozenset({"/a.txt", "/b.txt"})
        assert registry.tracked_path_count == 2

    def test_track_path_unregistered_transaction(self, registry: TransactionRegistry) -> None:
        # Path gets tracked but transaction has no path set
        result = registry.track_path("unknown-txn", "/file.txt")
        assert result is True
        assert registry.get_transaction_for_path("/file.txt") == "unknown-txn"

class TestFastPath:
    """Tests for has_active_transactions() fast-path."""

    def test_no_active_transactions_initially(self, registry: TransactionRegistry) -> None:
        assert registry.has_active_transactions() is False

    def test_has_active_after_register(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        assert registry.has_active_transactions() is True

    def test_no_active_after_unregister_all(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.unregister("txn-1")
        assert registry.has_active_transactions() is False

class TestGetPaths:
    """Tests for get_paths()."""

    def test_get_paths_empty(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        assert registry.get_paths("txn-1") == frozenset()

    def test_get_paths_unknown_transaction(self, registry: TransactionRegistry) -> None:
        assert registry.get_paths("unknown") == frozenset()

    def test_get_paths_returns_frozenset(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.track_path("txn-1", "/file.txt")
        paths = registry.get_paths("txn-1")
        assert isinstance(paths, frozenset)

class TestUnregister:
    """Tests for unregister()."""

    def test_unregister_cleans_paths(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.track_path("txn-1", "/a.txt")
        registry.track_path("txn-1", "/b.txt")

        removed = registry.unregister("txn-1")
        assert removed == frozenset({"/a.txt", "/b.txt"})
        assert registry.get_transaction_for_path("/a.txt") is None
        assert registry.get_transaction_for_path("/b.txt") is None
        assert registry.has_active_transactions() is False
        assert registry.tracked_path_count == 0

    def test_unregister_unknown_transaction(self, registry: TransactionRegistry) -> None:
        removed = registry.unregister("unknown")
        assert removed == frozenset()

    def test_unregister_allows_reuse_of_paths(self, registry: TransactionRegistry) -> None:
        registry.register("txn-1")
        registry.track_path("txn-1", "/file.txt")
        registry.unregister("txn-1")

        # Path should now be available for another transaction
        registry.register("txn-2")
        assert registry.track_path("txn-2", "/file.txt") is True

class TestConcurrent:
    """Thread-safety tests for TransactionRegistry."""

    def test_concurrent_register_unregister(self) -> None:
        """Stress test: many threads registering and unregistering."""
        registry = TransactionRegistry()
        num_threads = 20
        num_ops = 100

        def worker(tid: int) -> None:
            for i in range(num_ops):
                txn_id = f"txn-{tid}-{i}"
                registry.register(txn_id)
                registry.track_path(txn_id, f"/file-{tid}-{i}.txt")
                registry.unregister(txn_id)

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker, tid) for tid in range(num_threads)]
            for f in futures:
                f.result()

        assert registry.active_count == 0
        assert registry.tracked_path_count == 0

    def test_concurrent_track_conflict_detection(self) -> None:
        """Two threads racing to track the same path should detect conflict."""
        registry = TransactionRegistry()
        registry.register("txn-A")
        registry.register("txn-B")

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def track(txn_id: str) -> None:
            barrier.wait()
            result = registry.track_path(txn_id, "/contested.txt")
            results.append(result)

        t1 = threading.Thread(target=track, args=("txn-A",))
        t2 = threading.Thread(target=track, args=("txn-B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed, the other should fail
        assert sorted(results) == [False, True]
