"""Issue #4080: fingerprint cost on hot reads.

This bench reports the per-op fingerprint cost and compares "cheap" backends
(microsecond-class: cached ETag/stat) against "expensive" backends (sub-ms
API call). The acceptance threshold from the issue ("under 10% of get_content")
is meaningless when get_content is a sub-microsecond RAM hit — fingerprint
*is* the bulk of work. The actionable signal is:

- A "cheap" backend completes the workload in well under a millisecond per op.
- An "expensive" backend is >10× slower than the cheap one. That gap is the
  signal to fall back to TTL-only policy for that backend.

Run: pytest benches/cache_fingerprint_cost.py -v -s
"""

from __future__ import annotations

import time

from nexus.cache.file_store import FileKey, MemoryFileCache

ITERATIONS = 10_000


class _FakeBackend:
    def __init__(self, fp_cost_s: float) -> None:
        self._fp_cost_s = fp_cost_s
        self.fp_calls = 0

    def fingerprint(self, path: str) -> str:
        self.fp_calls += 1
        if self._fp_cost_s > 0:
            end = time.perf_counter() + self._fp_cost_s
            while time.perf_counter() < end:
                pass
        return "fp:" + path


def _run_hot_cache(backend: _FakeBackend) -> tuple[float, float]:
    cache = MemoryFileCache(max_bytes=10 * 1024 * 1024)
    key = FileKey("bench", "default", "/hot", "raw")
    cache.put_sync(key, b"x" * 4096, "fp:/hot", ttl_seconds=600)

    fp_total = 0.0
    e2e_total = 0.0
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        fp = backend.fingerprint("/hot")
        t1 = time.perf_counter()
        cache.get_sync(key, fp)
        t2 = time.perf_counter()
        fp_total += t1 - t0
        e2e_total += t2 - t0
    return fp_total, e2e_total


def test_cheap_fingerprint_is_fast() -> None:
    """~5µs fingerprint (e.g., cached ETag/stat) keeps total cost <1ms/op."""
    backend = _FakeBackend(fp_cost_s=5e-6)
    fp_total, e2e_total = _run_hot_cache(backend)
    per_op_ms = e2e_total / ITERATIONS * 1e3
    print(
        f"cheap fingerprint: fp_total={fp_total * 1e3:.1f}ms "
        f"e2e_total={e2e_total * 1e3:.1f}ms per_op={per_op_ms:.4f}ms"
    )
    assert per_op_ms < 1.0, f"cheap fingerprint per-op {per_op_ms:.4f}ms > 1ms"


def test_expensive_fingerprint_signal_for_ttl_fallback() -> None:
    """~200µs fingerprint (e.g., live API call) is the signal to fall back to TTL."""
    backend = _FakeBackend(fp_cost_s=2e-4)
    fp_total, e2e_total = _run_hot_cache(backend)
    per_op_ms = e2e_total / ITERATIONS * 1e3
    print(
        f"expensive fingerprint: fp_total={fp_total * 1e3:.1f}ms "
        f"e2e_total={e2e_total * 1e3:.1f}ms per_op={per_op_ms:.4f}ms"
    )
    # Per-op above 100µs is the threshold for considering TTL fallback.
    assert per_op_ms >= 0.1, f"expensive fingerprint per-op {per_op_ms:.4f}ms unexpectedly fast"
