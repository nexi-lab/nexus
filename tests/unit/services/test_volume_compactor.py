"""Tests for VolumeCompactor background service.

Tests:
  - Lifecycle: start/stop, double-start idempotency, is_running property
  - compact_once(): calls transport.compact(), returns metrics
  - Failure injection: transport.compact() raises → service continues
  - Interval: timer loop fires at configured intervals

Issue #3408: Volume compaction — reclaim space from deleted entries.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.volume_compactor import VolumeCompactor


class MockTransport:
    """Minimal BlobPackLocalTransport stub for compactor testing."""

    def __init__(self, compact_result=(0, 0, 0)):
        self._compact_result = compact_result
        self.compact_call_count = 0

    def compact(self):
        self.compact_call_count += 1
        return self._compact_result


class FailingTransport:
    """Transport stub that raises on compact()."""

    def compact(self):
        raise RuntimeError("Simulated compaction failure")


# ─── Lifecycle Tests ────────────────────────────────────────────────────────


class TestCompactorLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        transport = MockTransport()
        compactor = VolumeCompactor(transport, interval=0.1)

        assert not compactor.is_running

        await compactor.start()
        assert compactor.is_running

        await compactor.stop()
        assert not compactor.is_running

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        transport = MockTransport()
        compactor = VolumeCompactor(transport, interval=0.1)

        await compactor.start()
        await compactor.start()  # Should not raise or create duplicate tasks
        assert compactor.is_running

        await compactor.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        transport = MockTransport()
        compactor = VolumeCompactor(transport, interval=0.1)

        await compactor.stop()  # Should not raise
        assert not compactor.is_running


# ─── compact_once Tests ─────────────────────────────────────────────────────


class TestCompactOnce:
    @pytest.mark.asyncio
    async def test_compact_once_calls_transport(self):
        transport = MockTransport(compact_result=(2, 100, 50000))
        compactor = VolumeCompactor(transport, interval=300.0)

        result = await compactor.compact_once()

        assert result == (2, 100, 50000)
        assert transport.compact_call_count == 1

    @pytest.mark.asyncio
    async def test_compact_once_returns_zeros_on_noop(self):
        transport = MockTransport(compact_result=(0, 0, 0))
        compactor = VolumeCompactor(transport, interval=300.0)

        result = await compactor.compact_once()

        assert result == (0, 0, 0)


# ─── Failure Injection Tests ────────────────────────────────────────────────


class TestCompactorFailureInjection:
    @pytest.mark.asyncio
    async def test_compact_failure_doesnt_crash(self):
        transport = FailingTransport()
        compactor = VolumeCompactor(transport, interval=300.0)

        # Should not raise — failure is caught and logged
        result = await compactor.compact_once()
        assert result == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_loop_continues_after_failure(self):
        """The background loop should keep running after a compaction failure."""
        call_count = 0

        class CountingFailTransport:
            def compact(self):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("First call fails")
                return (1, 10, 5000)

        transport = CountingFailTransport()
        compactor = VolumeCompactor(transport, interval=0.05)

        await compactor.start()
        # Wait for at least 2 compaction cycles
        await asyncio.sleep(0.2)
        await compactor.stop()

        # Should have been called multiple times despite first failure
        assert call_count >= 2


# ─── Timer Interval Tests ───────────────────────────────────────────────────


class TestCompactorInterval:
    @pytest.mark.asyncio
    async def test_compaction_fires_on_interval(self):
        transport = MockTransport(compact_result=(1, 5, 1000))
        compactor = VolumeCompactor(transport, interval=0.05)

        await compactor.start()
        await asyncio.sleep(0.25)
        await compactor.stop()

        # Should have fired multiple times in 0.25s with 0.05s interval
        assert transport.compact_call_count >= 2

    @pytest.mark.asyncio
    async def test_default_interval(self):
        from nexus.services.volume_compactor import DEFAULT_COMPACTION_INTERVAL

        assert DEFAULT_COMPACTION_INTERVAL == 300.0


# ─── Concurrency Tests ─────────────────────────────────────────────────────


class TestCompactorConcurrency:
    @pytest.mark.asyncio
    async def test_default_max_concurrent_is_one(self):
        from nexus.services.volume_compactor import DEFAULT_MAX_CONCURRENT

        assert DEFAULT_MAX_CONCURRENT == 1

        transport = MockTransport()
        compactor = VolumeCompactor(transport)
        assert compactor.max_concurrent == 1

    @pytest.mark.asyncio
    async def test_custom_max_concurrent(self):
        transport = MockTransport()
        compactor = VolumeCompactor(transport, max_concurrent=4)
        assert compactor.max_concurrent == 4

    @pytest.mark.asyncio
    async def test_max_concurrent_minimum_is_one(self):
        transport = MockTransport()
        compactor = VolumeCompactor(transport, max_concurrent=0)
        assert compactor.max_concurrent == 1

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Verify that the semaphore actually limits concurrent compactions."""
        import time

        max_concurrent_seen = 0
        current_concurrent = 0

        class SlowTransport:
            def compact(self):
                nonlocal max_concurrent_seen, current_concurrent
                # Use a thread-safe approach since compact runs in a thread
                import threading

                with threading.Lock():
                    current_concurrent += 1
                    if current_concurrent > max_concurrent_seen:
                        max_concurrent_seen = current_concurrent
                time.sleep(0.05)
                with threading.Lock():
                    current_concurrent -= 1
                return (1, 1, 100)

        transport = SlowTransport()
        compactor = VolumeCompactor(transport, interval=300.0, max_concurrent=1)
        await compactor.start()

        # Fire multiple concurrent compact_once() calls
        tasks = [asyncio.create_task(compactor.compact_once()) for _ in range(3)]
        await asyncio.gather(*tasks)
        await compactor.stop()

        # With max_concurrent=1, only 1 should run at a time
        assert max_concurrent_seen == 1
