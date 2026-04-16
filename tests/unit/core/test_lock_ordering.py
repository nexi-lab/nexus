"""Lock ordering assertions and cross-zone deadlock tests (Issue #3392).

Validates:
    1. Debug-mode ordering assertions detect L2 → L1 violations.
    2. Observer context rejects L1/L2 acquisition.
    3. Cross-zone concurrent lock acquisition completes without deadlock.
    4. Multiple locks of the same layer are tracked with correct multiplicity.
    5. Nested observer contexts are handled correctly.

See: docs/architecture/LOCK-ORDERING.md
"""

import pytest

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


# Cross-zone concurrent locking tests moved to Rust (lock_manager.rs):
# - test_concurrent_reads, test_concurrent_write_exclusion
# - test_no_toctou_parent_child_write
# - advisory_hierarchy_* tests
