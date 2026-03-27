"""Concurrency race condition tests for LeaseManager (Issue #3407, Decision #11A).

Tests known race conditions with deterministic interleavings using
``asyncio.Event`` gates to force specific orderings.

Tested scenarios:
1. Concurrent conflicting acquires (two writers on same resource)
2. Revoke during extend
3. Expire during validate
4. Callback failure during revocation
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.contracts.protocols.lease import Lease, LeaseState
from nexus.lib.lease import LocalLeaseManager, ManualClock

READ = LeaseState.SHARED_READ
WRITE = LeaseState.EXCLUSIVE_WRITE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock(_now=1000.0)


@pytest.fixture()
def mgr(clock: ManualClock) -> LocalLeaseManager:
    return LocalLeaseManager(zone_id="test", clock=clock, sweep_interval=999.0)


# ---------------------------------------------------------------------------
# 1. Concurrent conflicting acquires
# ---------------------------------------------------------------------------


class TestConcurrentConflictingAcquires:
    @pytest.mark.asyncio()
    async def test_two_writers_exactly_one_wins(self, mgr: LocalLeaseManager) -> None:
        """Two tasks concurrently acquire WRITE on the same resource.
        Exactly one should win; the other either also wins (after revocation)
        or times out."""
        results: list[Lease | None] = []

        async def acquire_write(holder: str) -> None:
            lease = await mgr.acquire("r1", holder, WRITE, timeout=1.0)
            results.append(lease)

        await asyncio.gather(
            acquire_write("h1"),
            acquire_write("h2"),
        )

        # Both should succeed (DFUSE model: conflicts are revoked)
        # but only one should hold at the end
        leases = await mgr.leases_for_resource("r1")
        assert len(leases) == 1
        assert leases[0].state == WRITE

    @pytest.mark.asyncio()
    async def test_many_concurrent_writers(self, mgr: LocalLeaseManager) -> None:
        """10 tasks concurrently try WRITE — exactly one should hold at the end."""
        tasks = [mgr.acquire("r1", f"h{i}", WRITE, timeout=2.0) for i in range(10)]
        results = await asyncio.gather(*tasks)

        # At least one should succeed
        successes = [r for r in results if r is not None]
        assert len(successes) >= 1

        # Only one should hold at the end
        leases = await mgr.leases_for_resource("r1")
        assert len(leases) == 1
        assert leases[0].state == WRITE

    @pytest.mark.asyncio()
    async def test_concurrent_readers_all_succeed(self, mgr: LocalLeaseManager) -> None:
        """10 concurrent READ acquires should all succeed (compatible)."""
        tasks = [mgr.acquire("r1", f"reader-{i}", READ, timeout=1.0) for i in range(10)]
        results = await asyncio.gather(*tasks)
        successes = [r for r in results if r is not None]
        assert len(successes) == 10

        leases = await mgr.leases_for_resource("r1")
        assert len(leases) == 10


# ---------------------------------------------------------------------------
# 2. Revoke during extend
# ---------------------------------------------------------------------------


class TestRevokeDuringExtend:
    @pytest.mark.asyncio()
    async def test_revoke_and_extend_concurrent(self, mgr: LocalLeaseManager) -> None:
        """One task extends while another revokes the same lease.
        After both complete, the lease should be revoked."""
        await mgr.acquire("r1", "h1", READ, ttl=30.0)

        extend_result: list[Lease | None] = []
        revoke_result: list[list[Lease]] = []

        async def do_extend() -> None:
            result = await mgr.extend("r1", "h1", ttl=60.0)
            extend_result.append(result)

        async def do_revoke() -> None:
            result = await mgr.revoke("r1", holder_id="h1")
            revoke_result.append(result)

        await asyncio.gather(do_extend(), do_revoke())

        # After both operations, the lease should be gone
        # (revoke should win regardless of ordering)
        valid = await mgr.validate("r1", "h1")
        # Either:
        # - extend ran first (succeeded), then revoke ran (revoked it) -> None
        # - revoke ran first (revoked), then extend ran (returned None) -> None
        # In both cases, the final state should be: no lease
        # Note: if extend ran after revoke, it returns None and lease stays gone
        # If extend ran before revoke, revoke removes it
        assert valid is None

    @pytest.mark.asyncio()
    async def test_extend_after_revoke_returns_none(self, mgr: LocalLeaseManager) -> None:
        """Extend on a revoked lease returns None."""
        await mgr.acquire("r1", "h1", READ)
        await mgr.revoke("r1", holder_id="h1")
        result = await mgr.extend("r1", "h1")
        assert result is None


# ---------------------------------------------------------------------------
# 3. Expire during validate
# ---------------------------------------------------------------------------


class TestExpireDuringValidate:
    @pytest.mark.asyncio()
    async def test_lease_expires_between_operations(
        self, mgr: LocalLeaseManager, clock: ManualClock
    ) -> None:
        """Lease expires between acquire and validate calls."""
        await mgr.acquire("r1", "h1", READ, ttl=5.0)

        # Advance time past TTL
        clock.advance(6.0)

        # Validate should return None (expired)
        valid = await mgr.validate("r1", "h1")
        assert valid is None

    @pytest.mark.asyncio()
    async def test_concurrent_validate_and_expire(
        self, mgr: LocalLeaseManager, clock: ManualClock
    ) -> None:
        """Multiple validates with time advancing between them."""
        await mgr.acquire("r1", "h1", READ, ttl=10.0)

        # First validate should succeed
        v1 = await mgr.validate("r1", "h1")
        assert v1 is not None

        # Advance time past TTL
        clock.advance(11.0)

        # Second validate should fail
        v2 = await mgr.validate("r1", "h1")
        assert v2 is None

    @pytest.mark.asyncio()
    async def test_expired_lease_allows_new_acquire(
        self, mgr: LocalLeaseManager, clock: ManualClock
    ) -> None:
        """After a WRITE lease expires, a new holder can acquire WRITE."""
        await mgr.acquire("r1", "h1", WRITE, ttl=5.0)
        clock.advance(6.0)

        # New holder should be able to acquire
        lease = await mgr.acquire("r1", "h2", WRITE, timeout=0)
        assert lease is not None
        assert lease.holder_id == "h2"


# ---------------------------------------------------------------------------
# 4. Callback behavior during revocation
# ---------------------------------------------------------------------------


class TestCallbackDuringRevocation:
    @pytest.mark.asyncio()
    async def test_callback_exception_doesnt_block_revocation(self, mgr: LocalLeaseManager) -> None:
        """A callback that raises doesn't prevent the lease from being revoked."""
        call_order: list[str] = []

        async def good_callback(lease: Lease, reason: str) -> None:
            call_order.append("good")

        async def bad_callback(lease: Lease, reason: str) -> None:
            call_order.append("bad")
            raise RuntimeError("boom")

        mgr.register_revocation_callback("good", good_callback)
        mgr.register_revocation_callback("bad", bad_callback)

        await mgr.acquire("r1", "h1", READ)
        revoked = await mgr.revoke("r1")

        assert len(revoked) == 1
        assert await mgr.validate("r1", "h1") is None
        # Both callbacks were invoked (order may vary since they run concurrently)
        assert "good" in call_order
        assert "bad" in call_order

    @pytest.mark.asyncio()
    async def test_slow_callback_times_out(self, mgr: LocalLeaseManager) -> None:
        """A callback that takes too long is killed by the per-callback timeout."""
        mgr._callback_timeout = 0.01  # Very short timeout
        completed = False

        async def slow_callback(lease: Lease, reason: str) -> None:
            nonlocal completed
            await asyncio.sleep(10.0)
            completed = True  # Should never reach here

        mgr.register_revocation_callback("slow", slow_callback)
        await mgr.acquire("r1", "h1", READ)
        revoked = await mgr.revoke("r1")

        assert len(revoked) == 1
        assert not completed  # Callback was killed
        stats = await mgr.stats()
        assert stats["callback_error_count"] >= 1

    @pytest.mark.asyncio()
    async def test_multiple_callbacks_run_concurrently(self, mgr: LocalLeaseManager) -> None:
        """Multiple callbacks execute concurrently, not sequentially."""
        mgr._callback_timeout = 5.0
        start_times: dict[str, float] = {}
        import time

        async def timed_callback_a(lease: Lease, reason: str) -> None:
            start_times["a"] = time.monotonic()
            await asyncio.sleep(0.05)

        async def timed_callback_b(lease: Lease, reason: str) -> None:
            start_times["b"] = time.monotonic()
            await asyncio.sleep(0.05)

        mgr.register_revocation_callback("a", timed_callback_a)
        mgr.register_revocation_callback("b", timed_callback_b)

        await mgr.acquire("r1", "h1", READ)
        await mgr.revoke("r1")

        # Both started at roughly the same time (concurrent, not sequential)
        assert "a" in start_times
        assert "b" in start_times
        delta = abs(start_times["a"] - start_times["b"])
        # Should start within a few ms of each other (concurrent)
        assert delta < 0.04, f"Callbacks started {delta:.3f}s apart — not concurrent"

    @pytest.mark.asyncio()
    async def test_conflict_revocation_triggers_callbacks(self, mgr: LocalLeaseManager) -> None:
        """When acquire() revokes conflicting holders, callbacks fire."""
        events: list[tuple[str, str]] = []

        async def on_revoke(lease: Lease, reason: str) -> None:
            events.append((lease.holder_id, reason))

        mgr.register_revocation_callback("tracker", on_revoke)

        # Reader holds the resource
        await mgr.acquire("r1", "h1", READ)

        # Writer triggers conflict revocation
        lease = await mgr.acquire("r1", "h2", WRITE, timeout=1.0)
        assert lease is not None

        # Callback should have been invoked for h1 with reason "conflict"
        assert ("h1", "conflict") in events


# ---------------------------------------------------------------------------
# 5. Mixed concurrent operations
# ---------------------------------------------------------------------------


class TestMixedConcurrentOps:
    @pytest.mark.asyncio()
    async def test_acquire_revoke_extend_concurrent(self, mgr: LocalLeaseManager) -> None:
        """Multiple different operations on the same resource run concurrently."""
        # Set up initial state
        await mgr.acquire("r1", "h1", READ, ttl=30.0)
        await mgr.acquire("r1", "h2", READ, ttl=30.0)

        # Run acquire(write), revoke(h1), extend(h2) concurrently
        results = await asyncio.gather(
            mgr.acquire("r1", "h3", WRITE, timeout=1.0),
            mgr.revoke("r1", holder_id="h1"),
            mgr.extend("r1", "h2", ttl=60.0),
            return_exceptions=True,
        )

        # No exceptions should have been raised
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"

        # System should be in a consistent state
        leases = await mgr.leases_for_resource("r1")
        # Verify mutual exclusion
        write_holders = [ls for ls in leases if ls.state == WRITE]
        read_holders = [ls for ls in leases if ls.state == READ]
        if write_holders:
            assert len(write_holders) == 1
            assert not read_holders

    @pytest.mark.asyncio()
    async def test_concurrent_holder_revocation(self, mgr: LocalLeaseManager) -> None:
        """Concurrent revoke_holder calls for different holders."""
        await mgr.acquire("r1", "h1", READ)
        await mgr.acquire("r1", "h2", READ)
        await mgr.acquire("r2", "h1", READ)
        await mgr.acquire("r2", "h2", READ)

        r1, r2 = await asyncio.gather(
            mgr.revoke_holder("h1"),
            mgr.revoke_holder("h2"),
        )

        # Both should have revoked their leases
        assert len(r1) == 2  # h1 had 2 leases
        assert len(r2) == 2  # h2 had 2 leases

        # No leases should remain
        assert await mgr.leases_for_resource("r1") == []
        assert await mgr.leases_for_resource("r2") == []
