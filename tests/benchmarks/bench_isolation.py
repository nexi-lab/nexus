#!/usr/bin/env python3
"""Benchmark: IsolatedBackend overhead vs direct Backend calls.

Usage:
    uv run python benchmarks/bench_isolation.py

Measures ops/sec and p50/p95/p99 latency for:
    - Direct MockBackend calls (baseline)
    - IsolatedBackend with ProcessPoolExecutor
    - IsolatedBackend with InterpreterPoolExecutor (Python 3.14+ only)
"""

import hashlib
import sys
import time
from typing import Any

# ── Inline MockBackend (avoids importing test fixtures) ─────────────────


class BenchMockBackend:
    """Minimal in-memory backend for benchmarking."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._dirs: set[str] = {"/"}

    @property
    def name(self) -> str:
        return "bench_mock"

    @property
    def thread_safe(self) -> bool:
        return True

    def connect(self, context: Any = None) -> Any:  # noqa: ARG002
        from nexus.backends.base.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    def disconnect(self, context: Any = None) -> None:  # noqa: ARG002
        pass

    def check_connection(self, context: Any = None) -> Any:  # noqa: ARG002
        from nexus.backends.base.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: Any = None
    ) -> Any:  # noqa: ARG002
        from nexus.core.object_store import WriteResult

        h = hashlib.sha256(content).hexdigest()
        self._store[h] = content
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, content_hash: str, context: Any = None) -> bytes:  # noqa: ARG002
        if content_hash not in self._store:
            raise FileNotFoundError(f"CAS blob {content_hash} not found")
        return self._store[content_hash]

    def delete_content(self, content_hash: str, context: Any = None) -> None:  # noqa: ARG002
        self._store.pop(content_hash, None)

    def content_exists(self, content_hash: str, context: Any = None) -> bool:  # noqa: ARG002
        return content_hash in self._store

    def get_content_size(self, content_hash: str, context: Any = None) -> int:  # noqa: ARG002
        return len(self._store.get(content_hash, b""))

    def mkdir(
        self,
        path: str,
        parents: bool = False,  # noqa: ARG002
        exist_ok: bool = False,  # noqa: ARG002
        context: Any = None,  # noqa: ARG002
    ) -> None:
        self._dirs.add(path)

    def rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:  # noqa: ARG002
        self._dirs.discard(path)

    def is_directory(self, path: str, context: Any = None) -> bool:  # noqa: ARG002
        return path in self._dirs

    def list_dir(self, path: str, context: Any = None) -> list[str]:  # noqa: ARG002
        return []


# ── Benchmark harness ──────────────────────────────────────────────────


def _percentile(data: list[float], pct: float) -> float:
    """Return the *pct*-th percentile of *data* (0–100)."""
    sorted_data = sorted(data)
    idx = (pct / 100) * (len(sorted_data) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_data) - 1)
    frac = idx - lower
    return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac


def bench(label: str, func: Any, n: int = 1000) -> dict[str, float]:
    """Run *func* *n* times, report ops/sec and latency percentiles."""
    latencies: list[float] = []
    start = time.perf_counter()
    for _ in range(n):
        t0 = time.perf_counter()
        func()
        latencies.append(time.perf_counter() - t0)
    elapsed = time.perf_counter() - start
    ops = n / elapsed

    p50 = _percentile(latencies, 50) * 1000
    p95 = _percentile(latencies, 95) * 1000
    p99 = _percentile(latencies, 99) * 1000

    print(f"  {label:30s}  {ops:8.1f} ops/s  p50={p50:6.2f}ms  p95={p95:6.2f}ms  p99={p99:6.2f}ms")
    return {"ops": ops, "p50": p50, "p95": p95, "p99": p99}


def main() -> None:
    from nexus.bricks.sandbox.isolation import IsolatedBackend, IsolationConfig

    print(f"Python {sys.version}")
    print()

    # ── Prepare test data ────────────────────────────────────────────
    data_1kb = b"X" * 1024

    # ── Direct baseline ──────────────────────────────────────────────
    direct = BenchMockBackend()
    wr = direct.write_content(data_1kb)
    content_hash = wr.data

    print("=== Direct (no isolation) ===")
    n = 1000
    bench("write_content (1KB)", lambda: direct.write_content(data_1kb), n)
    bench("read_content (1KB)", lambda: direct.read_content(content_hash), n)
    bench("content_exists", lambda: direct.content_exists(content_hash), n)
    bench("list_dir", lambda: direct.list_dir("/"), n)
    print()

    # ── ProcessPoolExecutor ──────────────────────────────────────────
    iso_proc_cfg = IsolationConfig(
        backend_module="benchmarks.bench_isolation",
        backend_class="BenchMockBackend",
        pool_size=2,
        call_timeout=30.0,
        force_process=True,
    )
    iso_proc = IsolatedBackend(iso_proc_cfg)

    # Warm up (first call creates the pool + worker backend)
    wr_iso = iso_proc.write_content(data_1kb)
    iso_hash = wr_iso.data

    n_iso = 200  # fewer iterations due to IPC overhead
    print("=== ProcessPoolExecutor ===")
    bench("write_content (1KB)", lambda: iso_proc.write_content(data_1kb), n_iso)
    bench("read_content (1KB)", lambda: iso_proc.read_content(iso_hash), n_iso)
    bench("content_exists", lambda: iso_proc.content_exists(iso_hash), n_iso)
    bench("list_dir", lambda: iso_proc._pool.submit("list_dir", ("/",), {}), n_iso)
    iso_proc.close()
    print()

    # ── InterpreterPoolExecutor (3.14+ only) ─────────────────────────
    if sys.version_info >= (3, 14):
        iso_interp_cfg = IsolationConfig(
            backend_module="benchmarks.bench_isolation",
            backend_class="BenchMockBackend",
            pool_size=2,
            call_timeout=30.0,
            force_process=False,
        )
        iso_interp = IsolatedBackend(iso_interp_cfg)
        wr_interp = iso_interp.write_content(data_1kb)
        interp_hash = wr_interp.data

        print("=== InterpreterPoolExecutor ===")
        bench("write_content (1KB)", lambda: iso_interp.write_content(data_1kb), n_iso)
        bench("read_content (1KB)", lambda: iso_interp.read_content(interp_hash), n_iso)
        bench("content_exists", lambda: iso_interp.content_exists(interp_hash), n_iso)
        bench("list_dir", lambda: iso_interp._pool.submit("list_dir", ("/",), {}), n_iso)
        iso_interp.close()
    else:
        print("=== InterpreterPoolExecutor ===")
        print("  (skipped — requires Python 3.14+)")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
