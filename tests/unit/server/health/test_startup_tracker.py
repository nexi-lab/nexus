"""Tests for StartupTracker (#2168)."""

from __future__ import annotations

import time

from nexus.server.health.startup_tracker import (
    _ALL_PHASES,
    _REQUIRED_FOR_READY,
    StartupPhase,
    StartupTracker,
)


class TestStartupTracker:
    """Unit tests for StartupTracker."""

    def test_fresh_tracker_not_ready(self) -> None:
        tracker = StartupTracker()
        assert not tracker.is_ready
        assert not tracker.is_complete
        assert tracker.completed_phases == frozenset()
        assert tracker.pending_phases == _ALL_PHASES

    def test_complete_all_phases(self) -> None:
        tracker = StartupTracker()
        for phase in StartupPhase:
            tracker.complete(phase)
        assert tracker.is_complete
        assert tracker.is_ready
        assert tracker.pending_phases == frozenset()

    def test_ready_after_required_phases(self) -> None:
        tracker = StartupTracker()
        for phase in _REQUIRED_FOR_READY:
            tracker.complete(phase)
        assert tracker.is_ready
        assert not tracker.is_complete

    def test_not_ready_with_partial_required(self) -> None:
        tracker = StartupTracker()
        # Complete only one required phase
        tracker.complete(StartupPhase.OBSERVABILITY)
        assert not tracker.is_ready

    def test_phase_snapshots(self) -> None:
        tracker = StartupTracker()
        tracker.complete(StartupPhase.OBSERVABILITY)
        tracker.complete(StartupPhase.FEATURES)
        assert tracker.completed_phases == frozenset(
            {
                StartupPhase.OBSERVABILITY,
                StartupPhase.FEATURES,
            }
        )
        assert StartupPhase.OBSERVABILITY not in tracker.pending_phases
        assert StartupPhase.SERVICES in tracker.pending_phases

    def test_idempotent_complete(self) -> None:
        tracker = StartupTracker()
        tracker.complete(StartupPhase.OBSERVABILITY)
        tracker.complete(StartupPhase.OBSERVABILITY)
        assert len(tracker.completed_phases) == 1

    def test_elapsed_seconds(self) -> None:
        tracker = StartupTracker()
        time.sleep(0.05)
        assert tracker.elapsed_seconds >= 0.04
