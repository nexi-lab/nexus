"""Tests for fair-share admission control (Issue #1274).

Tests admission, counters, limits, sync, and snapshots.
"""

import pytest

from nexus.system_services.scheduler.policies.fair_share import FairShareCounter, FairShareSnapshot


class TestFairShareSnapshot:
    """Test FairShareSnapshot properties."""

    def test_available_slots(self):
        snap = FairShareSnapshot(agent_id="a", running_count=3, max_concurrent=10)
        assert snap.available_slots == 7

    def test_is_at_capacity(self):
        snap = FairShareSnapshot(agent_id="a", running_count=10, max_concurrent=10)
        assert snap.is_at_capacity is True

    def test_not_at_capacity(self):
        snap = FairShareSnapshot(agent_id="a", running_count=5, max_concurrent=10)
        assert snap.is_at_capacity is False

    def test_available_slots_never_negative(self):
        snap = FairShareSnapshot(agent_id="a", running_count=15, max_concurrent=10)
        assert snap.available_slots == 0


class TestFairShareAdmission:
    """Test admission checks."""

    def test_admit_under_limit(self):
        counter = FairShareCounter(default_max_concurrent=5)
        assert counter.admit("agent-a") is True

    def test_admit_at_limit_rejects(self):
        counter = FairShareCounter(default_max_concurrent=2)
        counter.record_start("agent-a")
        counter.record_start("agent-a")
        assert counter.admit("agent-a") is False

    def test_admit_does_not_increment(self):
        counter = FairShareCounter(default_max_concurrent=5)
        counter.admit("agent-a")
        counter.admit("agent-a")
        snap = counter.snapshot("agent-a")
        assert snap.running_count == 0  # admit is read-only


class TestFairShareCounters:
    """Test record_start and record_complete."""

    def test_start_increments(self):
        counter = FairShareCounter()
        counter.record_start("agent-a")
        assert counter.snapshot("agent-a").running_count == 1

    def test_complete_decrements(self):
        counter = FairShareCounter()
        counter.record_start("agent-a")
        counter.record_start("agent-a")
        counter.record_complete("agent-a")
        assert counter.snapshot("agent-a").running_count == 1

    def test_complete_never_negative(self):
        counter = FairShareCounter()
        counter.record_complete("agent-a")
        assert counter.snapshot("agent-a").running_count == 0


class TestFairShareLimits:
    """Test per-agent limit configuration."""

    def test_custom_limit(self):
        counter = FairShareCounter()
        counter.set_limit("agent-a", 3)
        snap = counter.snapshot("agent-a")
        assert snap.max_concurrent == 3

    def test_limit_enforced(self):
        counter = FairShareCounter()
        counter.set_limit("agent-a", 1)
        counter.record_start("agent-a")
        assert counter.admit("agent-a") is False

    def test_invalid_limit_raises(self):
        counter = FairShareCounter()
        with pytest.raises(ValueError, match="max_concurrent"):
            counter.set_limit("agent-a", 0)


class TestFairShareSync:
    """Test sync_from_db."""

    def test_sync_replaces_counters(self):
        counter = FairShareCounter()
        counter.record_start("agent-a")
        counter.sync_from_db({"agent-b": 5, "agent-c": 2})
        assert counter.snapshot("agent-a").running_count == 0
        assert counter.snapshot("agent-b").running_count == 5
        assert counter.snapshot("agent-c").running_count == 2

    def test_sync_empty_clears(self):
        counter = FairShareCounter()
        counter.record_start("agent-a")
        counter.sync_from_db({})
        assert counter.snapshot("agent-a").running_count == 0


class TestAllSnapshots:
    """Test all_snapshots aggregation."""

    def test_includes_running_and_limited_agents(self):
        counter = FairShareCounter()
        counter.record_start("agent-a")
        counter.set_limit("agent-b", 5)
        snaps = counter.all_snapshots()
        assert "agent-a" in snaps
        assert "agent-b" in snaps
        assert snaps["agent-a"].running_count == 1
        assert snaps["agent-b"].max_concurrent == 5
