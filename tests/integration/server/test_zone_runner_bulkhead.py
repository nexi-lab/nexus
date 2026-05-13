import asyncio
import statistics
import time

import pytest

from nexus.runtime.zone_runner import ZoneRegistry


@pytest.mark.asyncio
async def test_slow_zone_does_not_block_fast_zone_median_latency() -> None:
    registry = ZoneRegistry()
    try:
        slow_started = asyncio.Event()

        async def slow_work() -> str:
            slow_started.set()
            await asyncio.sleep(1.0)
            return "slow"

        async def fast_work() -> str:
            return "fast"

        slow_task = asyncio.create_task(registry.runner_for("zone-a").call(slow_work))
        await slow_started.wait()

        latencies: list[float] = []
        for _ in range(25):
            start = time.perf_counter()
            result = await registry.runner_for("zone-b").call(fast_work)
            latencies.append(time.perf_counter() - start)
            assert result == "fast"

        assert statistics.median(latencies) < 0.05
        assert await slow_task == "slow"
    finally:
        registry.stop_all()
