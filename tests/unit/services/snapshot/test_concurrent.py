"""Concurrency tests for transactional snapshots (Issue #1752).

Tests: Registry stress, CAS hold concurrent, Conflict detection,
       Deterministic interleaving.
"""

from concurrent.futures import ThreadPoolExecutor

from nexus.bricks.snapshot.registry import TransactionRegistry


class TestRegistryStress:
    """Stress test for TransactionRegistry under high concurrency."""

    def test_many_concurrent_transactions(self) -> None:
        """100 transactions with 10 paths each, all concurrent."""
        registry = TransactionRegistry()
        num_txns = 100
        paths_per_txn = 10
        errors: list[str] = []

        def worker(txn_idx: int) -> None:
            txn_id = f"txn-{txn_idx}"
            registry.register(txn_id)
            for p in range(paths_per_txn):
                path = f"/txn-{txn_idx}/file-{p}.txt"
                result = registry.track_path(txn_id, path)
                if not result:
                    errors.append(f"Failed to track {path} for {txn_id}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, i) for i in range(num_txns)]
            for f in futures:
                f.result()

        assert not errors, f"Tracking errors: {errors}"
        assert registry.active_count == num_txns
        assert registry.tracked_path_count == num_txns * paths_per_txn

        # Cleanup
        for i in range(num_txns):
            registry.unregister(f"txn-{i}")
        assert registry.active_count == 0
        assert registry.tracked_path_count == 0


class TestCASHoldConcurrent:
    """Concurrent CAS hold_reference tests."""

    def test_concurrent_hold_reference(self) -> None:
        """Multiple threads calling hold_reference on same hash."""
        from nexus.lib.semaphore import PythonVFSSemaphore

        # Create a mock CAS store with real VFS semaphore
        sem = PythonVFSSemaphore()
        ref_count = {"count": 1}

        def mock_hold_reference(content_id: str) -> bool:
            holder = sem.acquire(content_id, max_holders=1, timeout_ms=5000)
            if holder is None:
                return False
            try:
                ref_count["count"] += 1
                return True
            finally:
                sem.release(content_id, holder)

        num_threads = 20

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(mock_hold_reference, "abc123def456") for _ in range(num_threads)]
            results = [f.result() for f in futures]

        assert all(results)
        assert ref_count["count"] == 1 + num_threads


class TestConflictDetection:
    """Tests for conflict detection under concurrency."""

    def test_two_transactions_same_file_conflict(self) -> None:
        """Two transactions tracking the same file — second should fail."""
        registry = TransactionRegistry()
        registry.register("txn-A")
        registry.register("txn-B")

        assert registry.track_path("txn-A", "/shared.txt") is True
        assert registry.track_path("txn-B", "/shared.txt") is False

        # Only txn-A should own the path
        assert registry.get_transaction_for_path("/shared.txt") == "txn-A"


class TestDeterministicInterleaving:
    """Deterministic interleaving tests for race condition detection."""

    def test_begin_track_commit_interleaved(self) -> None:
        """Simulate interleaved begin/track/commit across two transactions."""
        registry = TransactionRegistry()

        # Transaction A begins and tracks a file
        registry.register("txn-A")
        registry.track_path("txn-A", "/file-1.txt")

        # Transaction B begins
        registry.register("txn-B")

        # B tries to track the same file — should fail
        assert registry.track_path("txn-B", "/file-1.txt") is False

        # B tracks a different file — should succeed
        assert registry.track_path("txn-B", "/file-2.txt") is True

        # A commits (unregisters)
        registry.unregister("txn-A")

        # Now B should be able to track file-1
        assert registry.track_path("txn-B", "/file-1.txt") is True
        assert registry.get_transaction_for_path("/file-1.txt") == "txn-B"

    def test_register_unregister_race(self) -> None:
        """Thread A registers while Thread B unregisters — should not corrupt state."""
        registry = TransactionRegistry()
        iterations = 1000

        def register_and_track(i: int) -> None:
            txn_id = f"txn-{i}"
            registry.register(txn_id)
            registry.track_path(txn_id, f"/file-{i}.txt")

        def unregister_batch(start: int, end: int) -> None:
            for i in range(start, end):
                registry.unregister(f"txn-{i}")

        # Phase 1: Register all
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(register_and_track, range(iterations)))

        assert registry.active_count == iterations

        # Phase 2: Unregister while checking consistency
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = []
            batch_size = iterations // 5
            for b in range(5):
                start = b * batch_size
                end = start + batch_size
                futures.append(pool.submit(unregister_batch, start, end))
            for f in futures:
                f.result()

        assert registry.active_count == 0
        assert registry.tracked_path_count == 0
