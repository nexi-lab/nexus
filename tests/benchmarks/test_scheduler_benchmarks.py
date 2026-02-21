"""Scheduler benchmarks (Issue #1274).

Performance benchmarks for HRRN ranking and classifier throughput.
Run with: uv run pytest tests/benchmarks/test_scheduler_benchmarks.py -v --override-ini="addopts="
"""

import time

import pytest

from nexus.services.scheduler.constants import PriorityTier, RequestState
from nexus.services.scheduler.policies.classifier import classify_request
from nexus.services.scheduler.policies.hrrn import compute_hrrn_score, rank_by_hrrn


@pytest.mark.benchmark
class TestHrrnRankingPerformance:
    """Benchmark HRRN ranking at scale."""

    def _make_tasks(self, n: int, now: float) -> list[dict]:
        return [
            {
                "id": f"task-{i}",
                "enqueued_at_epoch": now - (i * 0.1),
                "estimated_service_time": 10.0 + (i % 50),
            }
            for i in range(n)
        ]

    def test_rank_100_tasks(self):
        now = time.time()
        tasks = self._make_tasks(100, now)
        start = time.perf_counter()
        result = rank_by_hrrn(tasks, now)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(result) == 100
        assert elapsed_ms < 10, f"Ranking 100 tasks took {elapsed_ms:.2f}ms (>10ms)"

    def test_rank_1000_tasks(self):
        now = time.time()
        tasks = self._make_tasks(1000, now)
        start = time.perf_counter()
        result = rank_by_hrrn(tasks, now)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(result) == 1000
        assert elapsed_ms < 50, f"Ranking 1K tasks took {elapsed_ms:.2f}ms (>50ms)"

    def test_rank_10000_tasks(self):
        now = time.time()
        tasks = self._make_tasks(10000, now)
        start = time.perf_counter()
        result = rank_by_hrrn(tasks, now)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(result) == 10000
        assert elapsed_ms < 200, f"Ranking 10K tasks took {elapsed_ms:.2f}ms (>200ms)"


@pytest.mark.benchmark
class TestClassifierThroughput:
    """Benchmark classifier function throughput."""

    def test_classify_10000_requests(self):
        tiers = list(PriorityTier)
        states = list(RequestState)
        start = time.perf_counter()
        for i in range(10000):
            classify_request(tiers[i % len(tiers)], states[i % len(states)])
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 50, f"Classifying 10K requests took {elapsed_ms:.2f}ms (>50ms)"


@pytest.mark.benchmark
class TestHrrnScoreThroughput:
    """Benchmark HRRN score computation throughput."""

    def test_compute_100000_scores(self):
        start = time.perf_counter()
        for i in range(100000):
            compute_hrrn_score(float(i), 30.0)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 200, f"Computing 100K scores took {elapsed_ms:.2f}ms (>200ms)"
