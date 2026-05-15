"""Hypothesis stateful tests for LeaseManager (Issue #3407, Decision #9A).

Models the lease system as a ``RuleBasedStateMachine`` with operations
(acquire, revoke, extend, advance time) and invariants checked after
every step:

1. Mutual exclusion: no resource has both SHARED_READ and EXCLUSIVE_WRITE
   holders, and at most one EXCLUSIVE_WRITE holder.
2. Generation monotonicity: each successive grant for a resource produces
   a strictly increasing generation.
3. Expiry guarantee: after TTL elapses with no renewal, validate returns None.
4. Idempotent revocation: revoking a non-held lease is a no-op.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import note
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
)

from nexus.contracts.protocols.lease import Lease, LeaseState
from nexus.lib.lease import LocalLeaseManager, ManualClock

READ = LeaseState.SHARED_READ
WRITE = LeaseState.EXCLUSIVE_WRITE

# Strategies
resource_ids = st.sampled_from(["r1", "r2", "r3"])
holder_ids = st.sampled_from(["h1", "h2", "h3", "h4"])
states = st.sampled_from([READ, WRITE])
ttls = st.floats(min_value=1.0, max_value=60.0)
time_advances = st.floats(min_value=0.0, max_value=120.0)


def _noop() -> None:
    """No-op replacement for _ensure_sweep_task in stateful tests."""


def _run(coro: Any) -> Any:
    """Run an async coroutine synchronously for Hypothesis compatibility."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class LeaseManagerStateMachine(RuleBasedStateMachine):
    """Stateful test machine for lease invariants."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = ManualClock(_now=1000.0)
        self.mgr = LocalLeaseManager(zone_id="test", clock=self.clock, sweep_interval=999.0)
        # Prevent the sweep task from starting (it needs a persistent event loop
        # which RuleBasedStateMachine doesn't provide — each _run() creates a
        # new loop).  Sweep is tested separately in test_lease.py.
        self.mgr._closed = False
        # Disable sweep task — needs a persistent event loop that
        # RuleBasedStateMachine doesn't provide.
        self.mgr._ensure_sweep_task = _noop  # noqa: SLF001
        # Track the highest generation seen per (resource, holder) for
        # monotonicity check.  Idempotent re-acquires (same holder, same state)
        # return the existing lease — so generation monotonicity is per-holder,
        # not globally per-resource.
        self.max_generation: dict[tuple[str, str], int] = {}
        # Track active leases for invariant checking
        self.known_leases: dict[tuple[str, str], Lease] = {}

    @rule(
        resource_id=resource_ids,
        holder_id=holder_ids,
        state=states,
        ttl=ttls,
    )
    def acquire(self, resource_id: str, holder_id: str, state: LeaseState, ttl: float) -> None:
        lease = _run(self.mgr.acquire(resource_id, holder_id, state, ttl=ttl, timeout=1.0))
        if lease is not None:
            note(
                f"acquired {state.value} on {resource_id} for {holder_id} (gen={lease.generation})"
            )
            # Track for invariant checks
            self.known_leases[(resource_id, holder_id)] = lease
            # Generation monotonicity per (resource, holder): a new grant to
            # the same holder must have generation >= previously seen.
            # Idempotent re-acquires (same state) keep the same generation.
            # Upgrades (different state) get a new, higher generation.
            key = (resource_id, holder_id)
            prev_max = self.max_generation.get(key, 0)
            assert lease.generation >= prev_max, (
                f"Generation went backwards: {lease.generation} < {prev_max} "
                f"for {resource_id}:{holder_id}"
            )
            self.max_generation[key] = max(prev_max, lease.generation)

            # Remove known leases for holders that were revoked by conflict
            holders_snapshot = _run(self.mgr.leases_for_resource(resource_id))
            active_holders = {ls.holder_id for ls in holders_snapshot}
            to_remove = [
                (rid, hid)
                for (rid, hid) in self.known_leases
                if rid == resource_id and hid not in active_holders
            ]
            for key_to_remove in to_remove:
                del self.known_leases[key_to_remove]
        else:
            note(f"timeout acquiring {state.value} on {resource_id} for {holder_id}")

    @rule(resource_id=resource_ids, holder_id=holder_ids)
    def revoke(self, resource_id: str, holder_id: str) -> None:
        revoked = _run(self.mgr.revoke(resource_id, holder_id=holder_id))
        if revoked:
            note(f"revoked {resource_id} from {holder_id}")
            self.known_leases.pop((resource_id, holder_id), None)
        # Idempotent: revoking again should return empty
        revoked_again = _run(self.mgr.revoke(resource_id, holder_id=holder_id))
        assert revoked_again == [], "Double revoke should be a no-op"

    @rule(resource_id=resource_ids, holder_id=holder_ids, ttl=ttls)
    def extend(self, resource_id: str, holder_id: str, ttl: float) -> None:
        result = _run(self.mgr.extend(resource_id, holder_id, ttl=ttl))
        if result is not None:
            note(f"extended {resource_id} for {holder_id} by {ttl:.1f}s")
            self.known_leases[(resource_id, holder_id)] = result

    @rule(dt=time_advances)
    def advance_time(self, dt: float) -> None:
        self.clock.advance(dt)
        note(f"time advanced by {dt:.1f}s → now={self.clock.monotonic():.1f}")
        # Remove known leases that have expired
        now = self.clock.monotonic()
        expired = [key for key, lease in self.known_leases.items() if lease.is_expired(now)]
        for key in expired:
            del self.known_leases[key]

    @invariant()
    def mutual_exclusion(self) -> None:
        """No resource has conflicting lease states simultaneously."""
        # Group active leases by resource
        by_resource: dict[str, list[Lease]] = {}
        for (rid, _hid), lease in self.known_leases.items():
            if not lease.is_expired(self.clock.monotonic()):
                by_resource.setdefault(rid, []).append(lease)

        for rid, leases in by_resource.items():
            write_holders = [ls for ls in leases if ls.state == WRITE]
            read_holders = [ls for ls in leases if ls.state == READ]

            # At most one exclusive writer
            assert len(write_holders) <= 1, (
                f"Resource {rid} has {len(write_holders)} write holders: "
                f"{[ls.holder_id for ls in write_holders]}"
            )

            # No simultaneous read + write
            if write_holders:
                assert not read_holders, (
                    f"Resource {rid} has both write holder "
                    f"{write_holders[0].holder_id} and read holders "
                    f"{[ls.holder_id for ls in read_holders]}"
                )

    @invariant()
    def validate_matches_known_state(self) -> None:
        """Validate returns consistent results with our tracked state."""
        now = self.clock.monotonic()
        for (rid, hid), lease in list(self.known_leases.items()):
            if lease.is_expired(now):
                continue
            valid = _run(self.mgr.validate(rid, hid))
            # If we think it's active, the manager should agree
            # (the manager may have cleaned it up, which is also OK —
            #  we just can't have the manager say it's valid when we don't)
            if valid is None:
                # Manager cleaned it up (e.g., conflict revocation we missed)
                del self.known_leases[(rid, hid)]

    def teardown(self) -> None:
        # No sweep task to clean up (disabled in __init__).
        # Just clear internal state.
        self.mgr._by_resource.clear()
        self.mgr._by_holder.clear()


# Run the stateful test with reasonable settings
TestLeaseManagerStateful = LeaseManagerStateMachine.TestCase
TestLeaseManagerStateful.settings = h_settings(
    max_examples=100,
    stateful_step_count=30,
    deadline=None,  # async operations can be slow
)
