"""Issue #4080 acceptance bench: 90%+ hit rate on Zipf re-read workload.

Run: pytest benches/cache_hit_rate.py -v -s
"""

from __future__ import annotations

import asyncio
import random

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache

NUM_FILES = 1000
TOTAL_OPS = 10_000
WARMUP_OPS = 500
FILE_SIZES = [1024, 64 * 1024, 256 * 1024, 1024 * 1024]  # 1KB..1MB
CACHE_MAX_BYTES = 128 * 1024 * 1024  # 128 MB


class _Backend:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.calls = 0

    async def fetch(self, path: str) -> bytes:
        self.calls += 1
        await asyncio.sleep(0.001)
        return self._files[path]


def _make_files(rng: random.Random) -> dict[str, bytes]:
    return {f"/f/{i}": b"x" * rng.choice(FILE_SIZES) for i in range(NUM_FILES)}


def _zipf_index(rng: random.Random, n: int, alpha: float = 1.0) -> int:
    while True:
        x = rng.random()
        rank = int(1.0 / (x ** (1.0 / alpha)))
        if 0 < rank <= n:
            return rank - 1


@pytest.mark.asyncio
async def test_hit_rate_at_least_90_percent() -> None:
    rng = random.Random(42)
    files = _make_files(rng)
    backend = _Backend(files)
    cache = MemoryFileCache(max_bytes=CACHE_MAX_BYTES)

    hits = 0
    misses = 0

    for op in range(TOTAL_OPS):
        idx = _zipf_index(rng, NUM_FILES)
        path = f"/f/{idx}"
        key = FileKey("bench", "default", path, "raw")
        fp = f"fp:{path}"

        cached = await cache.get(key, fp)
        if cached is not None:
            if op >= WARMUP_OPS:
                hits += 1
            continue
        if op >= WARMUP_OPS:
            misses += 1

        lock = await cache.lock(key)
        async with lock:
            recheck = await cache.get(key, fp)
            if recheck is not None:
                continue
            content = await backend.fetch(path)
            await cache.put(key, content, fp, ttl_seconds=600)

    measured = hits + misses
    hit_rate = hits / measured if measured else 0.0
    print(
        f"hit_rate={hit_rate:.3f}  hits={hits}  misses={misses}  "
        f"backend_calls={backend.calls}  cache_bytes={cache.total_bytes}"
    )
    assert hit_rate >= 0.90, f"hit rate {hit_rate:.3f} below 0.90"


@pytest.mark.asyncio
async def test_singleflight_100_concurrent() -> None:
    files = {"/hot": b"payload" * 1000}
    backend = _Backend(files)
    cache = MemoryFileCache(max_bytes=CACHE_MAX_BYTES)
    key = FileKey("bench", "default", "/hot", "raw")
    fp = "fp:/hot"

    async def fetcher() -> bytes:
        lock = await cache.lock(key)
        async with lock:
            cached = await cache.get(key, fp)
            if cached is not None:
                return cached
            content = await backend.fetch("/hot")
            await cache.put(key, content, fp, ttl_seconds=600)
            return content

    results = await asyncio.gather(*[fetcher() for _ in range(100)])
    assert all(r == b"payload" * 1000 for r in results)
    assert backend.calls == 1, f"expected 1 backend call, got {backend.calls}"
