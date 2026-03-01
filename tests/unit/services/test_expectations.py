"""Tests for Expectations tracker — safe async convergence (Issue #2067).

TDD: Tests written FIRST, implementation follows.
Full 18-test matrix covering operations, satisfied, TTL, edge cases,
and integration with BrickReconciler.
"""

import threading
import time

import pytest

from nexus.contracts.protocols.brick_lifecycle import BrickState
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler
from nexus.system_services.lifecycle.expectations import (
    ExpectationEntry,
    Expectations,
)
from tests.unit.services.conftest import (
    make_lifecycle_brick as _make_lifecycle_brick,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def expectations() -> Expectations:
    """Default expectations tracker with standard TTL."""
    return Expectations()


@pytest.fixture
def short_ttl_expectations() -> Expectations:
    """Expectations tracker with very short TTL for expiration tests."""
    return Expectations(ttl=0.05)


@pytest.fixture
def manager() -> BrickLifecycleManager:
    return BrickLifecycleManager()


@pytest.fixture
def reconciler(manager: BrickLifecycleManager) -> BrickReconciler:
    return BrickReconciler(
        lifecycle_manager=manager,
        reconcile_interval=30.0,
        health_check_timeout=2.0,
        max_retries=3,
    )


# ---------------------------------------------------------------------------
# TestExpectationOperations (4 tests)
# ---------------------------------------------------------------------------


class TestExpectationOperations:
    """Test basic expect/observe operations."""

    def test_expect_mount_creates_entry(self, expectations: Expectations) -> None:
        """expect_mount creates an entry with pending_mounts=1."""
        expectations.expect_mount("brick-a")
        assert len(expectations) == 1
        assert not expectations.satisfied("brick-a")

    def test_expect_unmount_creates_entry(self, expectations: Expectations) -> None:
        """expect_unmount creates an entry with pending_unmounts=1."""
        expectations.expect_unmount("brick-b")
        assert len(expectations) == 1
        assert not expectations.satisfied("brick-b")

    def test_mount_observed_decrements(self, expectations: Expectations) -> None:
        """mount_observed decrements pending_mounts and cleans up when zero."""
        expectations.expect_mount("brick-a")
        assert not expectations.satisfied("brick-a")
        expectations.mount_observed("brick-a")
        assert expectations.satisfied("brick-a")
        assert len(expectations) == 0

    def test_unmount_observed_decrements(self, expectations: Expectations) -> None:
        """unmount_observed decrements pending_unmounts and cleans up when zero."""
        expectations.expect_unmount("brick-b")
        assert not expectations.satisfied("brick-b")
        expectations.unmount_observed("brick-b")
        assert expectations.satisfied("brick-b")
        assert len(expectations) == 0


# ---------------------------------------------------------------------------
# TestSatisfied (4 tests)
# ---------------------------------------------------------------------------


class TestSatisfied:
    """Test satisfied() predicate."""

    def test_satisfied_when_no_expectations(self, expectations: Expectations) -> None:
        """Key with no expectations is satisfied."""
        assert expectations.satisfied("unknown")

    def test_satisfied_when_all_observed(self, expectations: Expectations) -> None:
        """After all pending observed, key is satisfied."""
        expectations.expect_mount("brick-a")
        expectations.mount_observed("brick-a")
        assert expectations.satisfied("brick-a")

    def test_not_satisfied_with_pending_mount(self, expectations: Expectations) -> None:
        """Key with pending mount is not satisfied."""
        expectations.expect_mount("brick-a")
        assert not expectations.satisfied("brick-a")

    def test_not_satisfied_with_pending_unmount(self, expectations: Expectations) -> None:
        """Key with pending unmount is not satisfied."""
        expectations.expect_unmount("brick-a")
        assert not expectations.satisfied("brick-a")


# ---------------------------------------------------------------------------
# TestTTLExpiration (3 tests)
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    """Test TTL expiration behavior."""

    def test_expired_expectation_returns_satisfied(
        self, short_ttl_expectations: Expectations
    ) -> None:
        """After TTL expires, satisfied() returns True (K8s pattern)."""
        short_ttl_expectations.expect_mount("brick-a")
        assert not short_ttl_expectations.satisfied("brick-a")
        time.sleep(0.06)  # Wait for TTL to expire
        assert short_ttl_expectations.satisfied("brick-a")

    def test_non_expired_returns_not_satisfied(self, short_ttl_expectations: Expectations) -> None:
        """Before TTL, satisfied() returns False for pending entry."""
        short_ttl_expectations.expect_mount("brick-a")
        assert not short_ttl_expectations.satisfied("brick-a")

    def test_expire_stale_returns_removed_count(self, short_ttl_expectations: Expectations) -> None:
        """expire_stale() removes expired entries and returns count."""
        short_ttl_expectations.expect_mount("brick-a")
        short_ttl_expectations.expect_unmount("brick-b")
        assert len(short_ttl_expectations) == 2
        time.sleep(0.06)
        removed = short_ttl_expectations.expire_stale()
        assert removed == 2
        assert len(short_ttl_expectations) == 0


# ---------------------------------------------------------------------------
# TestEdgeCases (4 tests)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and safety."""

    def test_observe_without_expect_is_noop(self, expectations: Expectations) -> None:
        """Observing a key that was never expected is a harmless noop."""
        expectations.mount_observed("ghost")
        expectations.unmount_observed("ghost")
        assert len(expectations) == 0
        assert expectations.satisfied("ghost")

    def test_double_expect_overwrites(self, expectations: Expectations) -> None:
        """Second expect_mount overwrites the first entry."""
        expectations.expect_mount("brick-a")
        expectations.expect_mount("brick-a")
        assert len(expectations) == 1
        # Single observe should satisfy
        expectations.mount_observed("brick-a")
        assert expectations.satisfied("brick-a")

    def test_observe_past_zero_no_negative(self, expectations: Expectations) -> None:
        """Observing more than expected does not go negative."""
        expectations.expect_mount("brick-a")
        expectations.mount_observed("brick-a")
        expectations.mount_observed("brick-a")  # Extra observe
        assert expectations.satisfied("brick-a")
        assert len(expectations) == 0

    def test_concurrent_lock_safety(self, expectations: Expectations) -> None:
        """Concurrent expect/observe from multiple threads does not corrupt state."""
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            try:
                key = f"brick-{i % 5}"
                expectations.expect_mount(key)
                expectations.mount_observed(key)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All should be satisfied after observe
        for i in range(5):
            assert expectations.satisfied(f"brick-{i}")


# ---------------------------------------------------------------------------
# TestExpectationEntry (1 test — data model)
# ---------------------------------------------------------------------------


class TestExpectationEntry:
    """Test the frozen dataclass model."""

    def test_entry_is_frozen(self) -> None:
        """ExpectationEntry is immutable."""
        entry = ExpectationEntry(key="a", pending_mounts=1, created_at=1.0)
        with pytest.raises(AttributeError):
            entry.key = "b"


# ---------------------------------------------------------------------------
# TestExpectationsIntegration (3 tests)
# ---------------------------------------------------------------------------


class TestExpectationsIntegration:
    """Integration tests with BrickReconciler."""

    @pytest.mark.asyncio
    async def test_reconciler_skips_brick_with_pending_expectation(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Reconciler skips a brick that has unsatisfied expectations."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        # Don't mount — stays REGISTERED → would normally trigger MOUNT action

        # Manually set an expectation on the reconciler's tracker
        reconciler._expectations.expect_mount("a")

        result = await reconciler.reconcile()
        # Brick "a" should be skipped due to pending expectation
        a_drifts = [d for d in result.drifts if d.brick_name == "a"]
        assert len(a_drifts) == 0

    @pytest.mark.asyncio
    async def test_reconciler_auto_observes_from_snapshot(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Reconciler auto-observes completed mount operations from snapshot."""
        brick = _make_lifecycle_brick("a")
        manager.register("a", brick, protocol_name="AP")
        await manager.mount("a")
        status = manager.get_status("a")
        assert status is not None
        assert status.state == BrickState.ACTIVE

        # Set a mount expectation — brick is already ACTIVE
        reconciler._expectations.expect_mount("a")
        assert not reconciler._expectations.satisfied("a")

        # Reconcile should auto-observe the mount
        await reconciler.reconcile()
        assert reconciler._expectations.satisfied("a")

    @pytest.mark.asyncio
    async def test_full_cycle_expect_reconcile_observe(
        self, manager: BrickLifecycleManager, reconciler: BrickReconciler
    ) -> None:
        """Full lifecycle: expect → reconcile (skip) → observe → reconcile (act)."""
        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        # Brick is REGISTERED, spec.enabled=True → normally would mount

        # Step 1: Set expectation — reconciler should skip
        reconciler._expectations.expect_mount("a")
        result1 = await reconciler.reconcile()
        a_drifts1 = [d for d in result1.drifts if d.brick_name == "a"]
        assert len(a_drifts1) == 0  # Skipped

        # Step 2: Clear expectation manually (simulating observation)
        reconciler._expectations.mount_observed("a")
        assert reconciler._expectations.satisfied("a")

        # Step 3: Next reconcile should detect drift and mount
        result2 = await reconciler.reconcile()
        status = manager.get_status("a")
        assert status is not None
        assert status.state == BrickState.ACTIVE
        assert result2.actions_taken >= 1
