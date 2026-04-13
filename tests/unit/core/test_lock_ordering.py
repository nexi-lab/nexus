"""Lock ordering assertions and cross-zone deadlock tests (Issue #3392).

Validates:
    1. Debug-mode ordering assertions detect L2 → L1 violations.
    2. Observer context rejects L1/L2 acquisition.
    3. Cross-zone concurrent lock acquisition completes without deadlock.
    4. Multiple locks of the same layer are tracked with correct multiplicity.
    5. Nested observer contexts are handled correctly.

See: docs/architecture/LOCK-ORDERING.md
"""

import threading
import time

import pytest

from nexus.core.lock_fast import PythonVFSLockManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_lock_debug(monkeypatch):
    """Enable lock debug mode and reload the module."""
    monkeypatch.setenv("NEXUS_DEBUG_LOCK_ORDER", "1")
    import nexus.lib.lock_order as mod

    monkeypatch.setattr(mod, "LOCK_DEBUG_ENABLED", True)
    return mod


# ---------------------------------------------------------------------------
# Test 1: Layer ordering violation detection
# ---------------------------------------------------------------------------


class TestLockOrderingViolation:
    """Verify that acquiring L1 while holding L2 raises LockOrderError."""

    def test_l2_then_l1_raises(self, monkeypatch):
        """Holding L2 (advisory) and then acquiring L1 (VFS) is forbidden."""
        mod = _enable_lock_debug(monkeypatch)

        # Simulate holding an advisory lock (L2).
        mod.assert_can_acquire(mod.L2_ADVISORY)
        mod.mark_acquired(mod.L2_ADVISORY)

        # Attempting L1 while holding L2 must raise.
        with pytest.raises(mod.LockOrderError, match="L1:VFS.*L2:Advisory"):
            mod.assert_can_acquire(mod.L1_VFS)

        # Clean up.
        mod.mark_released(mod.L2_ADVISORY)

    def test_l1_then_l2_ok(self, monkeypatch):
        """Holding L1 (VFS) and then acquiring L2 (advisory) is permitted."""
        mod = _enable_lock_debug(monkeypatch)

        mod.assert_can_acquire(mod.L1_VFS)
        mod.mark_acquired(mod.L1_VFS)

        # L1 → L2 is the correct ordering — no exception.
        mod.assert_can_acquire(mod.L2_ADVISORY)

        mod.mark_released(mod.L1_VFS)

    def test_same_layer_ok(self, monkeypatch):
        """Acquiring the same layer twice is allowed (e.g. two VFS locks in rename)."""
        mod = _enable_lock_debug(monkeypatch)

        mod.assert_can_acquire(mod.L1_VFS)
        mod.mark_acquired(mod.L1_VFS)

        # Same layer — allowed (sorted-order rename pattern).
        mod.assert_can_acquire(mod.L1_VFS)

        mod.mark_released(mod.L1_VFS)

    def test_noop_when_disabled(self, monkeypatch):
        """Assertions are no-ops when NEXUS_DEBUG_LOCK_ORDER is not set."""
        import nexus.lib.lock_order as mod

        monkeypatch.setattr(mod, "LOCK_DEBUG_ENABLED", False)

        # Should not raise even with reversed ordering.
        mod.mark_acquired(mod.L2_ADVISORY)
        mod.assert_can_acquire(mod.L1_VFS)  # No-op — no exception.
        mod.mark_released(mod.L2_ADVISORY)


# ---------------------------------------------------------------------------
# Test 2: Multiplicity — partial release must not clear the layer
# ---------------------------------------------------------------------------


class TestLockMultiplicity:
    """Verify that multiple locks of the same layer are reference-counted."""

    def test_partial_release_keeps_layer(self, monkeypatch):
        """Acquiring L2 twice and releasing once must still track L2 as held."""
        mod = _enable_lock_debug(monkeypatch)

        # Acquire two advisory locks (e.g. on different paths).
        mod.mark_acquired(mod.L2_ADVISORY)
        mod.mark_acquired(mod.L2_ADVISORY)

        # Release one — L2 should still be tracked.
        mod.mark_released(mod.L2_ADVISORY)

        # L2 is still held, so L1 must be forbidden.
        with pytest.raises(mod.LockOrderError, match="L1:VFS.*L2:Advisory"):
            mod.assert_can_acquire(mod.L1_VFS)

        # Release the second — now L1 should be allowed.
        mod.mark_released(mod.L2_ADVISORY)
        mod.assert_can_acquire(mod.L1_VFS)

    def test_multiple_vfs_locks_rename_pattern(self, monkeypatch):
        """Rename acquires two L1 locks; releasing one must not clear L1."""
        mod = _enable_lock_debug(monkeypatch)

        mod.mark_acquired(mod.L1_VFS)
        mod.mark_acquired(mod.L1_VFS)

        # Release first lock.
        mod.mark_released(mod.L1_VFS)

        # L1 still held — same-layer re-acquire is fine.
        mod.assert_can_acquire(mod.L1_VFS)

        # Release second lock — fully clear.
        mod.mark_released(mod.L1_VFS)

    def test_release_without_acquire_is_safe(self, monkeypatch):
        """Releasing a layer that was never acquired should not crash."""
        mod = _enable_lock_debug(monkeypatch)

        # Should not raise or go negative.
        mod.mark_released(mod.L1_VFS)
        mod.mark_released(mod.L2_ADVISORY)


# ---------------------------------------------------------------------------
# Test 3: Observer context rejection
# ---------------------------------------------------------------------------


class TestObserverContextRejection:
    """Verify that observer tasks cannot acquire L1 or L2 locks."""

    def test_observer_cannot_acquire_l1(self, monkeypatch):
        """Observer context must reject VFS lock acquisition."""
        mod = _enable_lock_debug(monkeypatch)

        mod.enter_observer_context()
        try:
            with pytest.raises(mod.LockOrderError, match="observer.*L1:VFS"):
                mod.assert_can_acquire(mod.L1_VFS)
        finally:
            mod.exit_observer_context()

    def test_observer_cannot_acquire_l2(self, monkeypatch):
        """Observer context must reject advisory lock acquisition."""
        mod = _enable_lock_debug(monkeypatch)

        mod.enter_observer_context()
        try:
            with pytest.raises(mod.LockOrderError, match="observer.*L2:Advisory"):
                mod.assert_can_acquire(mod.L2_ADVISORY)
        finally:
            mod.exit_observer_context()

    def test_observer_can_acquire_l4(self, monkeypatch):
        """Observer context allows threading locks (L4) — observer pattern."""
        mod = _enable_lock_debug(monkeypatch)

        mod.enter_observer_context()
        try:
            # L4 is allowed in observer context.
            mod.assert_can_acquire(mod.L4_THREADING)
        finally:
            mod.exit_observer_context()

    def test_observer_context_scoped(self, monkeypatch):
        """Observer context is properly scoped — cleared after exit."""
        mod = _enable_lock_debug(monkeypatch)

        mod.enter_observer_context()
        assert mod.is_observer_context()
        mod.exit_observer_context()
        assert not mod.is_observer_context()

        # After exiting, L1 should be allowed again.
        mod.assert_can_acquire(mod.L1_VFS)


# ---------------------------------------------------------------------------
# Test 4: Nested observer context safety
# ---------------------------------------------------------------------------


class TestNestedObserverContext:
    """Verify that nested observer dispatch does not prematurely clear state."""

    def test_nested_enter_exit(self, monkeypatch):
        """Two enters require two exits to clear observer context."""
        mod = _enable_lock_debug(monkeypatch)

        mod.enter_observer_context()
        mod.enter_observer_context()

        # One exit — still in observer context.
        mod.exit_observer_context()
        assert mod.is_observer_context()

        # L1 still forbidden.
        with pytest.raises(mod.LockOrderError, match="observer"):
            mod.assert_can_acquire(mod.L1_VFS)

        # Second exit — now clear.
        mod.exit_observer_context()
        assert not mod.is_observer_context()
        mod.assert_can_acquire(mod.L1_VFS)

    def test_exit_without_enter_is_safe(self, monkeypatch):
        """Exiting observer context without entering should not go negative."""
        mod = _enable_lock_debug(monkeypatch)

        mod.exit_observer_context()
        assert not mod.is_observer_context()

        # Should still allow L1.
        mod.assert_can_acquire(mod.L1_VFS)


# ---------------------------------------------------------------------------
# Test 5: Cross-zone concurrent lock acquisition (deadlock-free)
# ---------------------------------------------------------------------------


class TestCrossZoneConcurrentLocking:
    """Verify that concurrent lock acquisition across zones completes
    without deadlock within a timeout."""

    def test_concurrent_vfs_locks_no_deadlock(self):
        """Multiple threads acquiring VFS locks on different paths must not deadlock.

        Simulates cross-zone concurrent access: each thread acquires locks
        on two paths in sorted order (the rename pattern).
        """
        mgr = PythonVFSLockManager()
        paths = [f"/zone-{i}/file.txt" for i in range(4)]
        errors: list[str] = []
        barrier = threading.Barrier(4, timeout=5)

        def worker(idx: int) -> None:
            try:
                barrier.wait()
                # Each worker acquires two locks in sorted order.
                p1, p2 = sorted([paths[idx], paths[(idx + 1) % len(paths)]])
                h1 = mgr.acquire(p1, "write", timeout_ms=2000)
                if h1 == 0:
                    errors.append(f"Worker {idx}: timeout acquiring {p1}")
                    return
                h2 = mgr.acquire(p2, "write", timeout_ms=2000) if p1 != p2 else 0
                if h2 == 0 and p1 != p2:
                    mgr.release(h1)
                    errors.append(f"Worker {idx}: timeout acquiring {p2}")
                    return
                # Hold briefly, then release.
                time.sleep(0.01)
                if h2:
                    mgr.release(h2)
                mgr.release(h1)
            except Exception as e:
                errors.append(f"Worker {idx}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"Deadlock detected: {len(alive)} threads still running"
        assert not errors, f"Lock errors: {errors}"

    def test_concurrent_rw_locks_no_starvation(self):
        """Readers and writers on the same path must all complete (no starvation).

        5 readers + 2 writers contending on the same path.
        """
        mgr = PythonVFSLockManager()
        results: list[tuple[str, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(7, timeout=5)

        def reader(idx: int) -> None:
            barrier.wait()
            h = mgr.acquire("/shared", "read", timeout_ms=5000)
            if h:
                time.sleep(0.005)
                mgr.release(h)
                with lock:
                    results.append(("read", idx))

        def writer(idx: int) -> None:
            barrier.wait()
            h = mgr.acquire("/shared", "write", timeout_ms=5000)
            if h:
                time.sleep(0.005)
                mgr.release(h)
                with lock:
                    results.append(("write", idx))

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        threads += [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"Starvation: {len(alive)} threads still running"
        assert len(results) == 7, f"Expected 7 completions, got {len(results)}"

    def test_concurrent_advisory_locks_no_deadlock(self):
        """Concurrent advisory lock acquisition across zones completes
        without deadlock."""
        import threading

        from nexus.lib.distributed_lock import LocalLockManager
        from nexus.lib.semaphore import create_vfs_semaphore

        sem = create_vfs_semaphore()
        managers = [LocalLockManager(sem) for i in range(3)]

        results: list[str] = []

        def worker(mgr: LocalLockManager, path: str, label: str) -> None:
            lock_id = mgr.acquire(path, mode="exclusive", timeout=5.0, ttl=5.0)
            if lock_id:
                time.sleep(0.01)
                mgr.release(lock_id, path)
                results.append(label)

        threads = [
            threading.Thread(target=worker, args=(managers[i], f"/file-{i}.txt", f"zone-{i}"))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 3, f"Expected 3 completions, got {results}"
