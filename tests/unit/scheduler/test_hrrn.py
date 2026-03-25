"""Tests for HRRN scoring (Issue #1274).

Tests score computation, edge cases, ranking, and property-based tests.
"""

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.system_services.scheduler.policies.hrrn import compute_hrrn_score, rank_by_hrrn


class TestComputeHrrnScore:
    """Test HRRN score computation."""

    def test_zero_wait_returns_one(self):
        assert compute_hrrn_score(0.0, 30.0) == pytest.approx(1.0)

    def test_wait_equals_service_returns_two(self):
        assert compute_hrrn_score(30.0, 30.0) == pytest.approx(2.0)

    def test_long_wait_increases_score(self):
        score = compute_hrrn_score(300.0, 30.0)
        assert score == pytest.approx(11.0)

    def test_negative_wait_clamped_to_zero(self):
        assert compute_hrrn_score(-10.0, 30.0) == pytest.approx(1.0)

    def test_zero_service_time_raises(self):
        with pytest.raises(ValueError, match="estimated_service_time"):
            compute_hrrn_score(10.0, 0.0)

    def test_negative_service_time_raises(self):
        with pytest.raises(ValueError, match="estimated_service_time"):
            compute_hrrn_score(10.0, -5.0)

    def test_small_service_time(self):
        score = compute_hrrn_score(100.0, 0.001)
        assert score > 1.0

    def test_score_always_gte_one(self):
        for wait in [0, 1, 10, 100, 1000, 10000]:
            for svc in [0.001, 1, 10, 30, 100]:
                assert compute_hrrn_score(float(wait), svc) >= 1.0


class TestRankByHrrn:
    """Test HRRN ranking."""

    def test_higher_wait_ranked_first(self):
        now = 1000.0
        tasks = [
            {"id": "A", "enqueued_at_epoch": 990.0, "estimated_service_time": 10.0},  # wait=10
            {"id": "B", "enqueued_at_epoch": 900.0, "estimated_service_time": 10.0},  # wait=100
        ]
        ranked = rank_by_hrrn(tasks, now)
        assert ranked[0]["id"] == "B"  # Higher wait → higher HRRN → first

    def test_shorter_service_ranked_higher_at_equal_wait(self):
        now = 1000.0
        tasks = [
            {"id": "A", "enqueued_at_epoch": 900.0, "estimated_service_time": 100.0},  # ratio=2.0
            {"id": "B", "enqueued_at_epoch": 900.0, "estimated_service_time": 10.0},  # ratio=11.0
        ]
        ranked = rank_by_hrrn(tasks, now)
        assert ranked[0]["id"] == "B"  # Short job gets higher ratio

    def test_empty_list(self):
        assert rank_by_hrrn([], 1000.0) == []

    def test_single_task(self):
        tasks = [{"id": "A", "enqueued_at_epoch": 900.0, "estimated_service_time": 30.0}]
        ranked = rank_by_hrrn(tasks, 1000.0)
        assert len(ranked) == 1
        assert ranked[0]["id"] == "A"

    def test_does_not_mutate_input(self):
        tasks = [
            {"id": "A", "enqueued_at_epoch": 990.0, "estimated_service_time": 10.0},
            {"id": "B", "enqueued_at_epoch": 900.0, "estimated_service_time": 10.0},
        ]
        original_order = [t["id"] for t in tasks]
        rank_by_hrrn(tasks, 1000.0)
        assert [t["id"] for t in tasks] == original_order


class TestHrrnHypothesis:
    """Property-based tests for HRRN scoring."""

    @given(wait=st.floats(0, 1e6), service=st.floats(0.001, 1e6))
    @settings(max_examples=200)
    def test_hrrn_score_always_gte_one(self, wait, service):
        assert compute_hrrn_score(wait, service) >= 1.0

    @given(wait=st.floats(0, 1e6), service=st.floats(0.001, 1e6))
    @settings(max_examples=200)
    def test_hrrn_score_increases_with_wait(self, wait, service):
        score1 = compute_hrrn_score(wait, service)
        score2 = compute_hrrn_score(wait + 100, service)
        assert score2 >= score1

    @given(service=st.floats(0.001, 1e6))
    @settings(max_examples=100)
    def test_zero_wait_equals_one(self, service):
        assert compute_hrrn_score(0.0, service) == pytest.approx(1.0)
