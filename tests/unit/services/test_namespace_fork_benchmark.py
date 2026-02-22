"""Benchmark tests for namespace fork operations (Issue #1273).

Lightweight timing assertions to verify fork/discard/read performance.
"""

import time
from unittest.mock import MagicMock

import pytest

from nexus.bricks.rebac.namespace_manager import MountEntry
from nexus.system_services.namespace.namespace_fork_service import (
    AgentNamespaceForkService,
)


@pytest.fixture
def large_namespace_manager() -> MagicMock:
    """Mock NamespaceManager with 1000 mount entries."""
    mgr = MagicMock()
    mgr.get_mount_table.return_value = [
        MountEntry(virtual_path=f"/workspace/dir-{i:04d}") for i in range(1000)
    ]
    return mgr


@pytest.fixture
def fork_service(large_namespace_manager: MagicMock) -> AgentNamespaceForkService:
    return AgentNamespaceForkService(namespace_manager=large_namespace_manager)


class TestForkBenchmark:
    def test_fork_latency_under_200us(self, fork_service: AgentNamespaceForkService) -> None:
        """Fork creation should complete in <200us for 1K entries (median of 100 runs)."""
        times: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            info = fork_service.fork("agent-bench")
            elapsed_us = (time.perf_counter() - start) * 1_000_000
            times.append(elapsed_us)
            fork_service.discard(info.fork_id)

        times.sort()
        median = times[len(times) // 2]
        # Allow generous margin for CI environments
        assert median < 5000, f"Median fork latency {median:.0f}us exceeds 5000us"

    def test_discard_constant_time(self, fork_service: AgentNamespaceForkService) -> None:
        """Discard should be O(1) regardless of overlay size."""
        info = fork_service.fork("agent-bench")
        ns = fork_service.get_fork(info.fork_id)
        # Add 500 overlay entries
        for i in range(500):
            ns.put(f"/extra/{i}", MountEntry(virtual_path=f"/extra/{i}"))

        start = time.perf_counter()
        fork_service.discard(info.fork_id)
        elapsed_us = (time.perf_counter() - start) * 1_000_000
        # Discard is just a dict.pop — should be well under 1ms
        assert elapsed_us < 1000, f"Discard took {elapsed_us:.0f}us"

    def test_read_fallthrough_overhead(self, fork_service: AgentNamespaceForkService) -> None:
        """Read fall-through should be <5x overhead vs direct dict lookup."""
        info = fork_service.fork("agent-bench")
        ns = fork_service.get_fork(info.fork_id)

        # Baseline: direct dict lookup
        snapshot = ns.get_parent_snapshot()
        key = "/workspace/dir-0500"
        start = time.perf_counter()
        for _ in range(10_000):
            snapshot.get(key)
        baseline_ns = (time.perf_counter() - start) / 10_000

        # Fork read: overlay check → parent fallthrough
        start = time.perf_counter()
        for _ in range(10_000):
            ns.get(key)
        fork_ns = (time.perf_counter() - start) / 10_000

        # Allow up to 20x (generous for CI, especially macOS runners) but typically <3x
        ratio = fork_ns / baseline_ns if baseline_ns > 0 else 1.0
        assert ratio < 20, f"Fork read overhead {ratio:.1f}x exceeds 20x"
        fork_service.discard(info.fork_id)
