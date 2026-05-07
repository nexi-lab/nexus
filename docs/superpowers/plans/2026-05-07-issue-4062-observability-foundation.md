# Issue 4062 Observability Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the #4062 foundation metrics surface for current read, write, batch, SQLite cache, and ETag paths, while defining stable Prometheus recorders for future foyer, prefetch, coalescing, generation, and passthrough work.

**Architecture:** Add a Python Prometheus catalog in `src/nexus/lib/io_metrics.py` and instrument the existing Python I/O wrapper at `src/nexus/core/nexus_fs_content.py`. Add a standalone `nexus-fuse` metrics module and metrics HTTP endpoint so the separate FUSE binary can be scraped independently. Add operator docs and a Grafana dashboard using the same metric names.

**Tech Stack:** Python 3.14, `prometheus_client`, pytest, Rust stable, `nexus-fuse` standalone Cargo crate, stdlib TCP listener for Prometheus text exposition, Grafana JSON provisioning.

---

## File Structure

Create:

- `src/nexus/lib/io_metrics.py`: Prometheus collectors and bounded recorder functions for I/O metrics.
- `tests/unit/services/test_io_metrics.py`: Unit tests for every Python recorder.
- `tests/integration/services/test_io_metrics_endpoint.py`: Scrape-level test that the catalog appears in Prometheus output.
- `tests/unit/core/test_nexus_fs_content_metrics.py`: Python I/O wrapper instrumentation tests with a fake Rust kernel boundary.
- `nexus-fuse/src/metrics.rs`: Standalone FUSE metrics state, Prometheus text renderer, and tiny HTTP server.
- `nexus-fuse/tests/metrics_test.rs`: Unit tests for FUSE metrics rendering and HTTP exposition.
- `docs/operations/nexus-io-observability.md`: Metric-to-question operator documentation.
- `observability/grafana/provisioning/dashboards/nexus-io-observability.json`: Standard Grafana dashboard.

Modify:

- `src/nexus/core/nexus_fs_content.py`: Record read latency/bytes, batch sizes, batch bytes, and backend write RPCs.
- `nexus-fuse/src/lib.rs`: Export `metrics`.
- `nexus-fuse/src/main.rs`: Add `--metrics-addr` / `NEXUS_FUSE_METRICS_ADDR` to `mount` and `daemon`, start the metrics server when present.
- `nexus-fuse/src/cache.rs`: Record SQLite cache hit/miss/stale and bytes-in-use metrics.
- `nexus-fuse/src/fs.rs`: Record ETag, read, and write metrics; return read tier from `read_cached()`.

## Task 1: Python Metrics Catalog

**Files:**

- Create: `src/nexus/lib/io_metrics.py`
- Create: `tests/unit/services/test_io_metrics.py`
- Create: `tests/integration/services/test_io_metrics_endpoint.py`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/services/test_io_metrics.py`:

```python
from __future__ import annotations

from prometheus_client import REGISTRY

from nexus.lib import io_metrics


def _sample(name: str, **labels: str) -> float:
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


def test_record_cache_request_increments_bounded_counter() -> None:
    before = _sample("nexus_cache_requests_total", tier="sqlite", result="hit")
    io_metrics.record_cache_request("sqlite", "hit")
    after = _sample("nexus_cache_requests_total", tier="sqlite", result="hit")
    assert after == before + 1


def test_unknown_cache_labels_collapse_to_other() -> None:
    before = _sample("nexus_cache_requests_total", tier="other", result="other")
    io_metrics.record_cache_request("tenant-123", "path-/secret")
    after = _sample("nexus_cache_requests_total", tier="other", result="other")
    assert after == before + 1


def test_cache_gauges_and_future_counters_update() -> None:
    io_metrics.set_cache_hit_ratio("sqlite", 0.75)
    assert _sample("nexus_cache_hit_ratio", tier="sqlite") == 0.75

    io_metrics.set_cache_bytes_in_use("sqlite", 1234)
    assert _sample("nexus_cache_bytes_in_use", tier="sqlite") == 1234

    before_evictions = _sample(
        "nexus_cache_evictions_total", tier="sqlite", reason="capacity"
    )
    io_metrics.record_cache_eviction("sqlite", "capacity")
    after_evictions = _sample(
        "nexus_cache_evictions_total", tier="sqlite", reason="capacity"
    )
    assert after_evictions == before_evictions + 1

    before_rejected = _sample("nexus_cache_admission_rejected_total")
    io_metrics.record_cache_admission_rejected()
    after_rejected = _sample("nexus_cache_admission_rejected_total")
    assert after_rejected == before_rejected + 1


def test_etag_recorders_update_expected_results() -> None:
    before_revalidate = _sample("nexus_cache_etag_revalidate_total", result="304")
    io_metrics.record_cache_etag_revalidate("304")
    after_revalidate = _sample("nexus_cache_etag_revalidate_total", result="304")
    assert after_revalidate == before_revalidate + 1

    before_check = _sample("nexus_etag_check_total", result="updated")
    io_metrics.record_etag_check("updated")
    after_check = _sample("nexus_etag_check_total", result="updated")
    assert after_check == before_check + 1


def test_prefetch_recorders_update_bounded_metrics() -> None:
    before_issued = _sample("nexus_prefetch_issued_bytes_total")
    io_metrics.record_prefetch_issued(10)
    assert _sample("nexus_prefetch_issued_bytes_total") == before_issued + 10

    before_used = _sample("nexus_prefetch_used_bytes_total")
    io_metrics.record_prefetch_used(7)
    assert _sample("nexus_prefetch_used_bytes_total") == before_used + 7

    before_wasted = _sample("nexus_prefetch_wasted_bytes_total")
    io_metrics.record_prefetch_wasted(3)
    assert _sample("nexus_prefetch_wasted_bytes_total") == before_wasted + 3

    io_metrics.set_prefetch_window_size(4096, mount="root", workspace="default")
    assert _sample("nexus_prefetch_window_size", mount="root", workspace="default") == 4096

    before_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="sequential")
    io_metrics.record_prefetch_pattern("sequential")
    after_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="sequential")
    assert after_pattern == before_pattern + 1


def test_read_metrics_update_bytes_and_histogram_count() -> None:
    before_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_count = _sample("nexus_read_latency_seconds_count", tier="backend")
    io_metrics.record_read(tier="backend", bytes_read=512, latency_seconds=0.005)
    assert _sample("nexus_read_bytes_total", tier="backend") == before_bytes + 512
    assert _sample("nexus_read_latency_seconds_count", tier="backend") == before_count + 1


def test_batch_write_and_consistency_recorders_update() -> None:
    before_batch = _sample("nexus_read_batch_size_count")
    io_metrics.record_read_batch_size(4)
    assert _sample("nexus_read_batch_size_count") == before_batch + 1

    before_passthrough = _sample("nexus_fuse_passthrough_used_total")
    io_metrics.record_fuse_passthrough_used()
    assert _sample("nexus_fuse_passthrough_used_total") == before_passthrough + 1

    before_flush = _sample("nexus_write_coalesce_flush_total", trigger="time")
    io_metrics.record_write_coalesce_flush("time")
    assert _sample("nexus_write_coalesce_flush_total", trigger="time") == before_flush + 1

    io_metrics.set_write_coalesce_dirty_bytes(2048)
    assert _sample("nexus_write_coalesce_dirty_bytes") == 2048

    before_rpc = _sample("nexus_write_backend_rpc_total")
    io_metrics.record_write_backend_rpc()
    assert _sample("nexus_write_backend_rpc_total") == before_rpc + 1

    before_mismatch = _sample("nexus_generation_mismatch_total")
    io_metrics.record_generation_mismatch()
    assert _sample("nexus_generation_mismatch_total") == before_mismatch + 1
```

Create `tests/integration/services/test_io_metrics_endpoint.py`:

```python
from __future__ import annotations

from prometheus_client import generate_latest

from nexus.lib import io_metrics


def test_io_metrics_exposed_via_global_registry() -> None:
    io_metrics.record_read(tier="backend", bytes_read=1, latency_seconds=0.001)
    io_metrics.record_cache_request("sqlite", "hit")
    io_metrics.record_write_backend_rpc()

    body = generate_latest().decode()

    expected_names = [
        "nexus_cache_requests_total",
        "nexus_cache_hit_ratio",
        "nexus_cache_evictions_total",
        "nexus_cache_bytes_in_use",
        "nexus_cache_admission_rejected_total",
        "nexus_cache_etag_revalidate_total",
        "nexus_prefetch_issued_bytes_total",
        "nexus_prefetch_used_bytes_total",
        "nexus_prefetch_wasted_bytes_total",
        "nexus_prefetch_window_size",
        "nexus_prefetch_pattern_detected_total",
        "nexus_read_latency_seconds",
        "nexus_read_bytes_total",
        "nexus_read_batch_size",
        "nexus_fuse_passthrough_used_total",
        "nexus_write_coalesce_flush_total",
        "nexus_write_coalesce_dirty_bytes",
        "nexus_write_backend_rpc_total",
        "nexus_generation_mismatch_total",
        "nexus_etag_check_total",
    ]
    for name in expected_names:
        assert name in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py -v
```

Expected: FAIL with `ImportError: cannot import name 'io_metrics' from 'nexus.services'`.

- [ ] **Step 3: Implement the Python metrics catalog**

Create `src/nexus/lib/io_metrics.py`:

```python
"""Prometheus metrics for Nexus read/write/cache hot paths (#4062)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

READ_LATENCY_BUCKETS = (
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

READ_BATCH_SIZE_BUCKETS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)

_CACHE_TIERS = frozenset({"sqlite", "dram", "nvme", "l1", "l2", "other"})
_CACHE_RESULTS = frozenset({"hit", "miss", "stale", "other"})
_CACHE_EVICTION_REASONS = frozenset({"capacity", "ttl", "manual", "other"})
_ETAG_RESULTS = frozenset({"304", "updated", "error", "fallback", "unexpected_304", "other"})
_PREFETCH_PATTERNS = frozenset({"sequential", "stride", "random", "majority_trend", "other"})
_READ_TIERS = frozenset(
    {"backend", "virtual", "error", "batch", "cache", "sqlite", "dram", "nvme", "passthrough", "other"}
)
_WRITE_FLUSH_TRIGGERS = frozenset({"time", "bytes", "close", "sync", "snapshot", "other"})
_SCOPES = frozenset({"default", "root", "local", "server", "fuse", "other"})


def _bounded(value: str | None, allowed: frozenset[str], default: str = "other") -> str:
    normalized = (value or default).strip().lower().replace("-", "_")
    return normalized if normalized in allowed else default


def _nonnegative(value: int | float) -> int | float:
    return value if value > 0 else 0


CACHE_REQUESTS = Counter(
    "nexus_cache_requests_total",
    "Total Nexus cache lookup requests.",
    ["tier", "result"],
)

CACHE_HIT_RATIO = Gauge(
    "nexus_cache_hit_ratio",
    "Current cache hit ratio for a bounded cache tier.",
    ["tier"],
)

CACHE_EVICTIONS = Counter(
    "nexus_cache_evictions_total",
    "Total Nexus cache evictions.",
    ["tier", "reason"],
)

CACHE_BYTES_IN_USE = Gauge(
    "nexus_cache_bytes_in_use",
    "Bytes currently held by a bounded cache tier.",
    ["tier"],
)

CACHE_ADMISSION_REJECTED = Counter(
    "nexus_cache_admission_rejected_total",
    "Total cache admissions rejected by the admission filter.",
)

CACHE_ETAG_REVALIDATE = Counter(
    "nexus_cache_etag_revalidate_total",
    "Total cache ETag revalidation attempts.",
    ["result"],
)

PREFETCH_ISSUED_BYTES = Counter(
    "nexus_prefetch_issued_bytes_total",
    "Total bytes requested by prefetch.",
)

PREFETCH_USED_BYTES = Counter(
    "nexus_prefetch_used_bytes_total",
    "Total prefetched bytes consumed by foreground reads.",
)

PREFETCH_WASTED_BYTES = Counter(
    "nexus_prefetch_wasted_bytes_total",
    "Total prefetched bytes evicted before use.",
)

PREFETCH_WINDOW_SIZE = Gauge(
    "nexus_prefetch_window_size",
    "Current adaptive prefetch window size for bounded scopes.",
    ["mount", "workspace"],
)

PREFETCH_PATTERN_DETECTED = Counter(
    "nexus_prefetch_pattern_detected_total",
    "Total detected prefetch access patterns.",
    ["pattern"],
)

READ_LATENCY = Histogram(
    "nexus_read_latency_seconds",
    "Nexus read latency in seconds.",
    ["tier"],
    buckets=READ_LATENCY_BUCKETS,
)

READ_BYTES = Counter(
    "nexus_read_bytes_total",
    "Total Nexus read bytes.",
    ["tier"],
)

READ_BATCH_SIZE = Histogram(
    "nexus_read_batch_size",
    "Number of paths requested in read_batch calls.",
    buckets=READ_BATCH_SIZE_BUCKETS,
)

FUSE_PASSTHROUGH_USED = Counter(
    "nexus_fuse_passthrough_used_total",
    "Total FUSE passthrough reads used.",
)

WRITE_COALESCE_FLUSH = Counter(
    "nexus_write_coalesce_flush_total",
    "Total write-coalescing flushes.",
    ["trigger"],
)

WRITE_COALESCE_DIRTY_BYTES = Gauge(
    "nexus_write_coalesce_dirty_bytes",
    "Current dirty bytes held by write coalescing.",
)

WRITE_BACKEND_RPC = Counter(
    "nexus_write_backend_rpc_total",
    "Total backend write RPCs issued by Nexus I/O paths.",
)

GENERATION_MISMATCH = Counter(
    "nexus_generation_mismatch_total",
    "Total cache invalidations caused by generation mismatch.",
)

ETAG_CHECKS = Counter(
    "nexus_etag_check_total",
    "Total ETag checks by result.",
    ["result"],
)


def record_cache_request(tier: str, result: str) -> None:
    CACHE_REQUESTS.labels(
        tier=_bounded(tier, _CACHE_TIERS),
        result=_bounded(result, _CACHE_RESULTS),
    ).inc()


def set_cache_hit_ratio(tier: str, ratio: float) -> None:
    bounded_ratio = max(0.0, min(float(ratio), 1.0))
    CACHE_HIT_RATIO.labels(tier=_bounded(tier, _CACHE_TIERS)).set(bounded_ratio)


def record_cache_eviction(tier: str, reason: str) -> None:
    CACHE_EVICTIONS.labels(
        tier=_bounded(tier, _CACHE_TIERS),
        reason=_bounded(reason, _CACHE_EVICTION_REASONS),
    ).inc()


def set_cache_bytes_in_use(tier: str, bytes_in_use: int) -> None:
    CACHE_BYTES_IN_USE.labels(tier=_bounded(tier, _CACHE_TIERS)).set(_nonnegative(bytes_in_use))


def record_cache_admission_rejected() -> None:
    CACHE_ADMISSION_REJECTED.inc()


def record_cache_etag_revalidate(result: str) -> None:
    CACHE_ETAG_REVALIDATE.labels(result=_bounded(result, _ETAG_RESULTS)).inc()


def record_etag_check(result: str) -> None:
    ETAG_CHECKS.labels(result=_bounded(result, _ETAG_RESULTS)).inc()


def record_prefetch_issued(bytes_count: int) -> None:
    PREFETCH_ISSUED_BYTES.inc(_nonnegative(bytes_count))


def record_prefetch_used(bytes_count: int) -> None:
    PREFETCH_USED_BYTES.inc(_nonnegative(bytes_count))


def record_prefetch_wasted(bytes_count: int) -> None:
    PREFETCH_WASTED_BYTES.inc(_nonnegative(bytes_count))


def set_prefetch_window_size(
    window_size: int,
    *,
    mount: str = "default",
    workspace: str = "default",
) -> None:
    PREFETCH_WINDOW_SIZE.labels(
        mount=_bounded(mount, _SCOPES, "default"),
        workspace=_bounded(workspace, _SCOPES, "default"),
    ).set(_nonnegative(window_size))


def record_prefetch_pattern(pattern: str) -> None:
    PREFETCH_PATTERN_DETECTED.labels(pattern=_bounded(pattern, _PREFETCH_PATTERNS)).inc()


def record_read(*, tier: str, bytes_read: int, latency_seconds: float) -> None:
    safe_tier = _bounded(tier, _READ_TIERS)
    READ_BYTES.labels(tier=safe_tier).inc(_nonnegative(bytes_read))
    READ_LATENCY.labels(tier=safe_tier).observe(float(_nonnegative(latency_seconds)))


def record_read_batch_size(count: int) -> None:
    READ_BATCH_SIZE.observe(float(_nonnegative(count)))


def record_fuse_passthrough_used() -> None:
    FUSE_PASSTHROUGH_USED.inc()


def record_write_coalesce_flush(trigger: str) -> None:
    WRITE_COALESCE_FLUSH.labels(trigger=_bounded(trigger, _WRITE_FLUSH_TRIGGERS)).inc()


def set_write_coalesce_dirty_bytes(bytes_count: int) -> None:
    WRITE_COALESCE_DIRTY_BYTES.set(_nonnegative(bytes_count))


def record_write_backend_rpc() -> None:
    WRITE_BACKEND_RPC.inc()


def record_generation_mismatch() -> None:
    GENERATION_MISMATCH.inc()
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/nexus/lib/io_metrics.py tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py
git commit -m "feat: add io metrics catalog"
```

## Task 2: Python I/O Wrapper Instrumentation

**Files:**

- Modify: `src/nexus/core/nexus_fs_content.py:17-18,64-144,521-607,714-855,1481-1726`
- Create: `tests/unit/core/test_nexus_fs_content_metrics.py`

- [ ] **Step 1: Write failing instrumentation tests**

Create `tests/unit/core/test_nexus_fs_content_metrics.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from prometheus_client import REGISTRY

from nexus.core.nexus_fs_content import ContentMixin


def _sample(name: str, **labels: str) -> float:
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


class _Kernel:
    def __init__(self) -> None:
        self.sys_read_calls = 0
        self.sys_write_calls = 0

    def sys_read(self, path: str, _ctx: object, _timeout_ms: int, _offset: int = 0) -> Any:
        self.sys_read_calls += 1
        return SimpleNamespace(
            data=b"abc",
            post_hook_needed=False,
            content_id="cid",
            entry_type=1,
            stream_next_offset=None,
        )

    def sys_write(self, path: str, _ctx: object, content: bytes, _offset: int = 0) -> Any:
        self.sys_write_calls += 1
        return SimpleNamespace(
            hit=True,
            content_id="cid",
            post_hook_needed=False,
            version=1,
            size=len(content),
            is_new=True,
            old_content_id=None,
            old_size=None,
            old_version=None,
            old_modified_at_ms=None,
        )

    def metastore_get_batch(self, paths: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                size=3,
                content_id=f"cid-{index}",
                version=1,
                modified_at=None,
            )
            for index, _path in enumerate(paths)
        ]

    def _read_batch(self, paths: list[str], _ctx: object) -> list[Any]:
        return [
            SimpleNamespace(
                data=f"b{index}".encode(),
                content_id=f"cid-{index}",
            )
            for index, _path in enumerate(paths)
        ]

    def hook_count(self, _name: str) -> int:
        return 0

    def dispatch_post_hooks(self, _name: str, _ctx: object) -> None:
        return None


class _Harness(ContentMixin):
    def __init__(self) -> None:
        self._kernel = _Kernel()
        self._zone_id = "root"
        self.metadata = SimpleNamespace()
        self._driver_coordinator = SimpleNamespace()

    def _parse_context(self, context: object | None) -> object | None:
        return context

    def resolve_read(self, path: str, *, context: object | None = None) -> tuple[bool, bytes | None]:
        return (False, None)

    def resolve_write(
        self, path: str, content: bytes, *, context: object | None = None
    ) -> tuple[bool, dict[str, object] | None]:
        return (False, None)

    def _build_rust_ctx(self, context: object | None, is_admin: bool) -> object:
        return object()

    def _get_context_identity(self, context: object | None) -> tuple[str, str | None, bool]:
        return ("root", None, False)

    def _validate_path(self, path: str) -> str:
        return path

    def _batch_permission_check(self, paths: list[str], context: object | None) -> set[str]:
        return set(paths)

    def _dispatch_batch_post_hook(self, _name: str, _ctx: object) -> None:
        return None


class _VirtualHarness(_Harness):
    def resolve_read(self, path: str, *, context: object | None = None) -> tuple[bool, bytes | None]:
        return (True, b"virtual")


def test_sys_read_records_backend_latency_and_bytes() -> None:
    harness = _Harness()
    before_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_count = _sample("nexus_read_latency_seconds_count", tier="backend")

    assert harness.sys_read("/file.txt") == b"abc"

    assert _sample("nexus_read_bytes_total", tier="backend") == before_bytes + 3
    assert _sample("nexus_read_latency_seconds_count", tier="backend") == before_count + 1


def test_sys_read_records_virtual_resolver_reads() -> None:
    harness = _VirtualHarness()
    before_bytes = _sample("nexus_read_bytes_total", tier="virtual")

    assert harness.sys_read("/__sys__/virtual") == b"virtual"

    assert _sample("nexus_read_bytes_total", tier="virtual") == before_bytes + 7


def test_sys_write_records_backend_rpc_when_kernel_hit() -> None:
    harness = _Harness()
    before = _sample("nexus_write_backend_rpc_total")

    harness.sys_write("/file.txt", b"abc")

    assert _sample("nexus_write_backend_rpc_total") == before + 1


def test_write_locked_records_backend_rpc_when_kernel_hit() -> None:
    harness = _Harness()
    before = _sample("nexus_write_backend_rpc_total")

    harness._write_locked("/file.txt", b"abc")

    assert _sample("nexus_write_backend_rpc_total") == before + 1


def test_read_batch_records_batch_size_and_batch_bytes() -> None:
    harness = _Harness()
    before_size = _sample("nexus_read_batch_size_count")
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")

    results = harness.read_batch(["/a.txt", "/b.txt"])

    assert [item["content"] for item in results] == [b"b0", b"b1"]
    assert _sample("nexus_read_batch_size_count") == before_size + 1
    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes + 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/unit/core/test_nexus_fs_content_metrics.py -v
```

Expected: FAIL because `src/nexus/core/nexus_fs_content.py` does not record `nexus_read_bytes_total`, `nexus_read_latency_seconds`, `nexus_read_batch_size`, or `nexus_write_backend_rpc_total`.

- [ ] **Step 3: Import the metrics module**

Modify imports in `src/nexus/core/nexus_fs_content.py` near the existing imports:

```python
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    ConflictError,
    NexusFileNotFoundError,
)
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose
from nexus.lib import io_metrics
```

- [ ] **Step 4: Instrument `sys_read()`**

Wrap the existing `sys_read()` body with a timer and record on every return path. Preserve the existing read hook behavior. The resulting method body should follow this structure:

```python
        start = time.perf_counter()
        try:
            context = self._parse_context(context)
            _handled, _resolve_hint = self.resolve_read(path, context=context)
            if _handled:
                content = _resolve_hint or b""
                if offset or count is not None:
                    content = (
                        content[offset : offset + count]
                        if count is not None
                        else content[offset:]
                    )
                io_metrics.record_read(
                    tier="virtual",
                    bytes_read=len(content),
                    latency_seconds=time.perf_counter() - start,
                )
                return content

            _is_admin = (
                getattr(context, "is_admin", False)
                if context is not None and not isinstance(context, dict)
                else (context.get("is_admin", False) if isinstance(context, dict) else False)
            )

            if self._kernel is None:
                raise NexusFileNotFoundError(path)
            _rust_ctx = self._build_rust_ctx(context, _is_admin)
            _timeout_ms = 5000
            result = self._kernel.sys_read(path, _rust_ctx, _timeout_ms, offset)

            if result.entry_type == 4:
                payload = bytes(result.data) if result.data else b""
                io_metrics.record_read(
                    tier="backend",
                    bytes_read=len(payload),
                    latency_seconds=time.perf_counter() - start,
                )
                return {
                    "data": payload,
                    "next_offset": result.stream_next_offset or 0,
                }

            data = result.data or b""

            if offset or count is not None:
                data = data[offset : offset + count] if count is not None else data[offset:]

            if result.post_hook_needed:
                zone_id, agent_id, _ = self._get_context_identity(context)
                from nexus.contracts.vfs_hooks import ReadHookContext

                _read_ctx = ReadHookContext(
                    path=path,
                    context=context,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    content=data,
                    content_id=result.content_id,
                )
                self._kernel.dispatch_post_hooks("read", _read_ctx)
                data = _read_ctx.content or data

            io_metrics.record_read(
                tier="backend",
                bytes_read=len(data),
                latency_seconds=time.perf_counter() - start,
            )
            return data
        except Exception:
            io_metrics.record_read(
                tier="error",
                bytes_read=0,
                latency_seconds=time.perf_counter() - start,
            )
            raise
```

- [ ] **Step 5: Instrument `sys_write()` and `_write_locked()`**

In `sys_write()`, immediately after `result = self._kernel.sys_write(path, _rust_ctx, buf, offset)`, add:

```python
        if result.hit:
            io_metrics.record_write_backend_rpc()
```

In `_write_locked()`, immediately after `result = self._kernel.sys_write(path, _rust_ctx, buf, offset)`, add the same block:

```python
        if result.hit:
            io_metrics.record_write_backend_rpc()
```

- [ ] **Step 6: Instrument `read_batch()`**

At the start of `read_batch()`, before the empty-list return, record the requested batch size:

```python
        io_metrics.record_read_batch_size(len(paths))
        if not paths:
            return []
```

Inside the fallback branch where `content = self.read(path, context=context)` succeeds, immediately after `_loaded_bytes += len(content)`, add:

```python
                    io_metrics.record_read(
                        tier="batch",
                        bytes_read=len(content),
                        latency_seconds=0.0,
                    )
```

Inside the Rust-result branch, immediately after `_loaded_bytes += len(content)`, add:

```python
            io_metrics.record_read(
                tier="batch",
                bytes_read=len(content),
                latency_seconds=0.0,
            )
```

Use `latency_seconds=0.0` for per-item batch byte accounting so `nexus_read_bytes_total{tier="batch"}` is accurate without pretending each item has an independent latency measurement.

- [ ] **Step 7: Run tests to verify they pass**

Run:

```bash
pytest tests/unit/core/test_nexus_fs_content_metrics.py tests/unit/services/test_io_metrics.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/nexus/core/nexus_fs_content.py tests/unit/core/test_nexus_fs_content_metrics.py
git commit -m "feat: record python io metrics"
```

## Task 3: FUSE Metrics Core And Endpoint

**Files:**

- Create: `nexus-fuse/src/metrics.rs`
- Create: `nexus-fuse/tests/metrics_test.rs`
- Modify: `nexus-fuse/src/lib.rs:5-9`
- Modify: `nexus-fuse/src/main.rs:6-220`

- [ ] **Step 1: Write failing FUSE metrics tests**

Create `nexus-fuse/tests/metrics_test.rs`:

```rust
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use nexus_fuse::metrics;

#[test]
fn render_includes_recorded_counter_gauge_and_histogram() {
    metrics::reset_for_tests();

    metrics::record_cache_request("sqlite", "hit");
    metrics::set_cache_bytes_in_use("sqlite", 4096);
    metrics::record_read("cache", 128, Duration::from_millis(2));
    metrics::record_write_backend_rpc();

    let body = metrics::render();

    assert!(body.contains("nexus_cache_requests_total{tier=\"sqlite\",result=\"hit\"} 1"));
    assert!(body.contains("nexus_cache_bytes_in_use{tier=\"sqlite\"} 4096"));
    assert!(body.contains("nexus_read_bytes_total{tier=\"cache\"} 128"));
    assert!(body.contains("nexus_read_latency_seconds_count{tier=\"cache\"} 1"));
    assert!(body.contains("nexus_write_backend_rpc_total 1"));
}

#[test]
fn unknown_labels_collapse_to_other() {
    metrics::reset_for_tests();

    metrics::record_cache_request("path-/secret", "tenant-123");

    let body = metrics::render();
    assert!(body.contains("nexus_cache_requests_total{tier=\"other\",result=\"other\"} 1"));
}

#[test]
fn metrics_server_returns_prometheus_text() {
    metrics::reset_for_tests();
    metrics::record_write_backend_rpc();

    let server = metrics::start_server("127.0.0.1:0").expect("metrics server should bind");
    let addr = server.local_addr();

    let mut stream = TcpStream::connect(addr).expect("connect metrics server");
    stream
        .write_all(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");

    let mut body = String::new();
    stream.read_to_string(&mut body).expect("read response");
    assert!(body.contains("HTTP/1.1 200 OK"));
    assert!(body.contains("nexus_write_backend_rpc_total 1"));
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml --test metrics_test
```

Expected: FAIL with `unresolved import nexus_fuse::metrics`.

- [ ] **Step 3: Implement `nexus-fuse/src/metrics.rs`**

Create `nexus-fuse/src/metrics.rs`:

```rust
//! Prometheus text metrics for the standalone nexus-fuse binary (#4062).

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener};
use std::sync::{LazyLock, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const READ_LATENCY_BUCKETS: &[f64] = &[
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

#[derive(Clone, Debug)]
struct HistogramState {
    buckets: &'static [f64],
    bucket_counts: Vec<u64>,
    count: u64,
    sum: f64,
}

impl HistogramState {
    fn new(buckets: &'static [f64]) -> Self {
        Self {
            buckets,
            bucket_counts: vec![0; buckets.len()],
            count: 0,
            sum: 0.0,
        }
    }

    fn observe(&mut self, value: f64) {
        let safe = if value.is_finite() && value > 0.0 { value } else { 0.0 };
        self.count += 1;
        self.sum += safe;
        for (idx, bucket) in self.buckets.iter().enumerate() {
            if safe <= *bucket {
                self.bucket_counts[idx] += 1;
            }
        }
    }
}

#[derive(Default)]
struct MetricsState {
    cache_requests: HashMap<(String, String), u64>,
    cache_etag_revalidate: HashMap<String, u64>,
    etag_checks: HashMap<String, u64>,
    cache_bytes_in_use: HashMap<String, u64>,
    read_bytes: HashMap<String, u64>,
    read_latency: HashMap<String, HistogramState>,
    write_backend_rpc_total: u64,
}

static METRICS: LazyLock<Mutex<MetricsState>> =
    LazyLock::new(|| Mutex::new(MetricsState::default()));

pub struct MetricsServer {
    local_addr: SocketAddr,
    _thread: JoinHandle<()>,
}

impl MetricsServer {
    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }
}

fn bounded(value: &str, allowed: &[&str]) -> String {
    let normalized = value.trim().to_ascii_lowercase().replace('-', "_");
    if allowed.iter().any(|item| *item == normalized) {
        normalized
    } else {
        "other".to_string()
    }
}

fn cache_tier(value: &str) -> String {
    bounded(value, &["sqlite", "dram", "nvme", "l1", "l2", "other"])
}

fn cache_result(value: &str) -> String {
    bounded(value, &["hit", "miss", "stale", "other"])
}

fn etag_result(value: &str) -> String {
    bounded(value, &["304", "updated", "error", "fallback", "unexpected_304", "other"])
}

fn read_tier(value: &str) -> String {
    bounded(
        value,
        &["backend", "virtual", "error", "batch", "cache", "sqlite", "dram", "nvme", "passthrough", "other"],
    )
}

pub fn reset_for_tests() {
    *METRICS.lock().unwrap() = MetricsState::default();
}

pub fn record_cache_request(tier: &str, result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .cache_requests
        .entry((cache_tier(tier), cache_result(result)))
        .or_insert(0) += 1;
}

pub fn record_cache_etag_revalidate(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .cache_etag_revalidate
        .entry(etag_result(result))
        .or_insert(0) += 1;
}

pub fn record_etag_check(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics.etag_checks.entry(etag_result(result)).or_insert(0) += 1;
}

pub fn set_cache_bytes_in_use(tier: &str, bytes: u64) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.cache_bytes_in_use.insert(cache_tier(tier), bytes);
}

pub fn record_read(tier: &str, bytes: usize, latency: Duration) {
    let safe_tier = read_tier(tier);
    let mut metrics = METRICS.lock().unwrap();
    *metrics.read_bytes.entry(safe_tier.clone()).or_insert(0) += bytes as u64;
    metrics
        .read_latency
        .entry(safe_tier)
        .or_insert_with(|| HistogramState::new(READ_LATENCY_BUCKETS))
        .observe(latency.as_secs_f64());
}

pub fn record_write_backend_rpc() {
    let mut metrics = METRICS.lock().unwrap();
    metrics.write_backend_rpc_total += 1;
}

fn write_counter_line(out: &mut String, name: &str, labels: &str, value: u64) {
    if labels.is_empty() {
        out.push_str(&format!("{name} {value}\n"));
    } else {
        out.push_str(&format!("{name}{{{labels}}} {value}\n"));
    }
}

pub fn render() -> String {
    let metrics = METRICS.lock().unwrap();
    let mut out = String::new();

    out.push_str("# TYPE nexus_cache_requests_total counter\n");
    for ((tier, result), value) in &metrics.cache_requests {
        write_counter_line(
            &mut out,
            "nexus_cache_requests_total",
            &format!("tier=\"{tier}\",result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_cache_bytes_in_use gauge\n");
    for (tier, value) in &metrics.cache_bytes_in_use {
        out.push_str(&format!(
            "nexus_cache_bytes_in_use{{tier=\"{tier}\"}} {value}\n"
        ));
    }

    out.push_str("# TYPE nexus_cache_etag_revalidate_total counter\n");
    for (result, value) in &metrics.cache_etag_revalidate {
        write_counter_line(
            &mut out,
            "nexus_cache_etag_revalidate_total",
            &format!("result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_etag_check_total counter\n");
    for (result, value) in &metrics.etag_checks {
        write_counter_line(
            &mut out,
            "nexus_etag_check_total",
            &format!("result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_read_bytes_total counter\n");
    for (tier, value) in &metrics.read_bytes {
        write_counter_line(
            &mut out,
            "nexus_read_bytes_total",
            &format!("tier=\"{tier}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_read_latency_seconds histogram\n");
    for (tier, histogram) in &metrics.read_latency {
        for (idx, bucket) in histogram.buckets.iter().enumerate() {
            out.push_str(&format!(
                "nexus_read_latency_seconds_bucket{{tier=\"{tier}\",le=\"{bucket}\"}} {}\n",
                histogram.bucket_counts[idx]
            ));
        }
        out.push_str(&format!(
            "nexus_read_latency_seconds_bucket{{tier=\"{tier}\",le=\"+Inf\"}} {}\n",
            histogram.count
        ));
        out.push_str(&format!(
            "nexus_read_latency_seconds_sum{{tier=\"{tier}\"}} {}\n",
            histogram.sum
        ));
        out.push_str(&format!(
            "nexus_read_latency_seconds_count{{tier=\"{tier}\"}} {}\n",
            histogram.count
        ));
    }

    out.push_str("# TYPE nexus_write_backend_rpc_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_write_backend_rpc_total",
        "",
        metrics.write_backend_rpc_total,
    );

    out
}

fn handle_client(mut stream: std::net::TcpStream) {
    let mut buffer = [0_u8; 1024];
    let _ = stream.read(&mut buffer);
    let body = render();
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/plain; version=0.0.4; charset=utf-8\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
}

pub fn start_server(addr: &str) -> std::io::Result<MetricsServer> {
    let listener = TcpListener::bind(addr)?;
    let local_addr = listener.local_addr()?;
    let thread = thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            handle_client(stream);
        }
    });
    Ok(MetricsServer {
        local_addr,
        _thread: thread,
    })
}
```

- [ ] **Step 4: Export the module**

Modify `nexus-fuse/src/lib.rs`:

```rust
pub mod cache;
pub mod client;
pub mod daemon;
pub mod error;
pub mod fs;
pub mod metrics;
```

- [ ] **Step 5: Add CLI metrics endpoint wiring**

Modify `nexus-fuse/src/main.rs` imports:

```rust
use clap::{Parser, Subcommand};
use fuser::MountOption;
use log::{error, info};
use nexus_fuse::{cache, client, daemon, fs, metrics};
use std::path::PathBuf;
```

Add `metrics_addr` to both `Mount` and `Daemon` command variants:

```rust
        /// Prometheus metrics bind address, for example 127.0.0.1:9464
        #[arg(long, env = "NEXUS_FUSE_METRICS_ADDR")]
        metrics_addr: Option<String>,
```

Bind the new field in both match arms. In the `Mount` arm, start the server after resolving the API key:

```rust
            let _metrics_server = if let Some(addr) = metrics_addr.as_deref() {
                let server = metrics::start_server(addr)?;
                info!("FUSE metrics listening on {}", server.local_addr());
                Some(server)
            } else {
                None
            };
```

In the `Daemon` arm, use the same block before creating `DaemonConfig`. Keep the `_metrics_server` binding in scope for the whole arm so the server thread handle is not dropped.

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml --test metrics_test
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add nexus-fuse/src/metrics.rs nexus-fuse/src/lib.rs nexus-fuse/src/main.rs nexus-fuse/tests/metrics_test.rs
git commit -m "feat: add nexus-fuse metrics endpoint"
```

## Task 4: FUSE Cache, ETag, Read, And Write Instrumentation

**Files:**

- Modify: `nexus-fuse/src/cache.rs:8-13,145-205,221-336`
- Modify: `nexus-fuse/src/fs.rs:3-15,369-440,591-697,900-940`

- [ ] **Step 1: Add failing cache metric tests inside `cache.rs`**

Inside the existing `#[cfg(test)] mod tests` in `nexus-fuse/src/cache.rs`, add:

```rust
    #[test]
    fn test_cache_get_records_hit_miss_and_stale_metrics() {
        crate::metrics::reset_for_tests();
        let cache = test_cache("metrics");

        assert!(matches!(cache.get("/metrics-miss.txt"), CacheLookup::Miss));
        assert!(crate::metrics::render()
            .contains("nexus_cache_requests_total{tier=\"sqlite\",result=\"miss\"} 1"));

        cache.put("/metrics-hit.txt", b"data", Some("etag-1"));
        assert!(matches!(cache.get("/metrics-hit.txt"), CacheLookup::Hit(_)));
        assert!(crate::metrics::render()
            .contains("nexus_cache_requests_total{tier=\"sqlite\",result=\"hit\"} 1"));

        let old_cached_at = FileCache::now().saturating_sub(MAX_CACHE_AGE_SECS + 1);
        {
            let conn = cache.conn.lock().unwrap();
            conn.execute(
                "UPDATE file_cache SET cached_at = ? WHERE path = ?",
                params![old_cached_at, "/metrics-hit.txt"],
            )
            .unwrap();
        }
        assert!(matches!(
            cache.get("/metrics-hit.txt"),
            CacheLookup::NeedsRevalidation { .. }
        ));
        assert!(crate::metrics::render()
            .contains("nexus_cache_requests_total{tier=\"sqlite\",result=\"stale\"} 1"));
    }

    #[test]
    fn test_cache_put_records_bytes_in_use() {
        crate::metrics::reset_for_tests();
        let cache = test_cache("bytes");

        cache.put("/metrics-bytes.txt", b"data", Some("etag-1"));

        assert!(crate::metrics::render().contains("nexus_cache_bytes_in_use{tier=\"sqlite\"} 4"));
    }
```

- [ ] **Step 2: Run cache tests to verify they fail**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml cache::tests::test_cache_get_records_hit_miss_and_stale_metrics
cargo test --manifest-path nexus-fuse/Cargo.toml cache::tests::test_cache_put_records_bytes_in_use
```

Expected: FAIL because `FileCache::get()` and `FileCache::put()` do not call `crate::metrics`.

- [ ] **Step 3: Instrument `FileCache::get()`, `put()`, and `stats()`**

In `nexus-fuse/src/cache.rs`, add `use crate::metrics;` near the imports.

In `FileCache::get()`, record each result immediately before returning:

```rust
                            metrics::record_cache_request("sqlite", "hit");
                            CacheLookup::Hit(CacheEntry { content, etag })
```

```rust
                            metrics::record_cache_request("sqlite", "miss");
                            CacheLookup::Miss
```

```rust
                    metrics::record_cache_request("sqlite", "stale");
                    CacheLookup::NeedsRevalidation { etag }
```

```rust
                    metrics::record_cache_request("sqlite", "miss");
                    CacheLookup::Miss
```

```rust
                metrics::record_cache_request("sqlite", "miss");
                CacheLookup::Miss
```

In `FileCache::put()`, after a successful insert, call `self.stats()` and update bytes:

```rust
            let stats = self.stats();
            metrics::set_cache_bytes_in_use("sqlite", stats.total_size);
```

In `FileCache::stats()`, before returning `CacheStats`, also set the gauge:

```rust
        metrics::set_cache_bytes_in_use("sqlite", total_size);

        CacheStats {
            file_count,
            total_size,
        }
```

- [ ] **Step 4: Run cache tests to verify they pass**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml cache::tests::test_cache_get_records_hit_miss_and_stale_metrics
cargo test --manifest-path nexus-fuse/Cargo.toml cache::tests::test_cache_put_records_bytes_in_use
```

Expected: PASS.

- [ ] **Step 5: Instrument `fs.rs` read cache result tiers**

In `nexus-fuse/src/fs.rs`, add `metrics` to the imports:

```rust
use crate::cache::{CacheLookup, FileCache};
use crate::client::{FileEntry, NexusClient, ReadResponse};
use crate::metrics;
```

Add a result struct above `impl NexusFs`:

```rust
#[derive(Debug)]
struct ReadCachedResult {
    content: Vec<u8>,
    etag: Option<String>,
    tier: &'static str,
}
```

Change `read_cached()` signature:

```rust
    fn read_cached(&self, path: &str) -> anyhow::Result<ReadCachedResult> {
```

Update the hit return:

```rust
                    return Ok(ReadCachedResult {
                        content: entry.content,
                        etag: entry.etag,
                        tier: "cache",
                    });
```

Update the 304 path before returning:

```rust
                                    metrics::record_cache_etag_revalidate("304");
                                    metrics::record_etag_check("304");
                                    return Ok(ReadCachedResult {
                                        content: entry.content,
                                        etag: entry.etag,
                                        tier: "cache",
                                    });
```

Update the revalidation-with-content path:

```rust
                            metrics::record_cache_etag_revalidate("updated");
                            metrics::record_etag_check("updated");
                            cache.put(path, &content, etag.as_deref());
                            return Ok(ReadCachedResult {
                                content,
                                etag,
                                tier: "backend",
                            });
```

Update the revalidation error fallback:

```rust
                            metrics::record_cache_etag_revalidate("fallback");
                            metrics::record_etag_check("fallback");
                            if let CacheLookup::Hit(entry) = cache.get(path) {
                                return Ok(ReadCachedResult {
                                    content: entry.content,
                                    etag: entry.etag,
                                    tier: "cache",
                                });
                            }
                            metrics::record_cache_etag_revalidate("error");
                            metrics::record_etag_check("error");
                            return Err(e.into());
```

Update the normal backend fetch:

```rust
                Ok(ReadCachedResult {
                    content,
                    etag,
                    tier: "backend",
                })
```

Update the unexpected 304 path:

```rust
                metrics::record_etag_check("unexpected_304");
                Err(anyhow::anyhow!("Unexpected 304 response"))
```

- [ ] **Step 6: Update `read_cached()` callers**

In `read()`, replace tuple handling with the struct:

```rust
        let started_at = std::time::Instant::now();

        let read_result = match self.read_cached(&path) {
            Ok(result) => result,
            Err(e) => {
                metrics::record_read("error", 0, started_at.elapsed());
                if e.downcast_ref::<crate::error::NexusClientError>()
                    .map_or(false, |ne| ne.is_not_found())
                {
                    reply.error(ENOENT);
                } else {
                    error!("read error for {}: {}", path, e);
                    reply.error(EIO);
                }
                return;
            }
        };

        let content = read_result.content;
```

Immediately after calculating and returning the requested slice, record bytes and latency:

```rust
        if offset >= content.len() {
            metrics::record_read(read_result.tier, 0, started_at.elapsed());
            reply.data(&[]);
        } else {
            let end = std::cmp::min(offset + size as usize, content.len());
            let slice = &content[offset..end];
            metrics::record_read(read_result.tier, slice.len(), started_at.elapsed());
            reply.data(slice);
        }
```

In partial write handling, replace:

```rust
                Ok((data, _)) => data,
```

with:

```rust
                Ok(result) => result.content,
```

In truncate handling, replace:

```rust
                    Ok((mut data, _)) => {
```

with:

```rust
                    Ok(result) => {
                        let mut data = result.content;
```

In both successful `self.client.write` branches in `write()`, add before invalidation:

```rust
                    metrics::record_write_backend_rpc();
```

Also add `metrics::record_write_backend_rpc();` for the successful truncate writes in `setattr()` where `self.client.write` reaches the backend.

- [ ] **Step 7: Run focused FUSE tests**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml cache
cargo test --manifest-path nexus-fuse/Cargo.toml --test metrics_test
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add nexus-fuse/src/cache.rs nexus-fuse/src/fs.rs
git commit -m "feat: instrument nexus-fuse io metrics"
```

## Task 5: Operator Documentation And Grafana Dashboard

**Files:**

- Create: `docs/operations/nexus-io-observability.md`
- Create: `observability/grafana/provisioning/dashboards/nexus-io-observability.json`

- [ ] **Step 1: Write docs and dashboard validation tests**

Create `tests/unit/services/test_io_metrics_docs.py`:

```python
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "operations" / "nexus-io-observability.md"
DASHBOARD = (
    ROOT
    / "observability"
    / "grafana"
    / "provisioning"
    / "dashboards"
    / "nexus-io-observability.json"
)

METRIC_NAMES = [
    "nexus_cache_requests_total",
    "nexus_cache_hit_ratio",
    "nexus_cache_evictions_total",
    "nexus_cache_bytes_in_use",
    "nexus_cache_admission_rejected_total",
    "nexus_cache_etag_revalidate_total",
    "nexus_prefetch_issued_bytes_total",
    "nexus_prefetch_used_bytes_total",
    "nexus_prefetch_wasted_bytes_total",
    "nexus_prefetch_window_size",
    "nexus_prefetch_pattern_detected_total",
    "nexus_read_latency_seconds",
    "nexus_read_bytes_total",
    "nexus_read_batch_size",
    "nexus_fuse_passthrough_used_total",
    "nexus_write_coalesce_flush_total",
    "nexus_write_coalesce_dirty_bytes",
    "nexus_write_backend_rpc_total",
    "nexus_generation_mismatch_total",
    "nexus_etag_check_total",
]


def test_io_observability_docs_cover_every_metric() -> None:
    body = DOC.read_text()
    for metric in METRIC_NAMES:
        assert metric in body


def test_grafana_dashboard_is_valid_and_mentions_core_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["uid"] == "nexus-io-observability"
    assert dashboard["title"] == "Nexus I/O Observability"
    encoded = json.dumps(dashboard)
    for metric in [
        "nexus_read_latency_seconds",
        "nexus_cache_requests_total",
        "nexus_etag_check_total",
        "nexus_write_backend_rpc_total",
    ]:
        assert metric in encoded
```

- [ ] **Step 2: Run docs test to verify it fails**

Run:

```bash
pytest tests/unit/services/test_io_metrics_docs.py -v
```

Expected: FAIL because the docs and dashboard files do not exist.

- [ ] **Step 3: Write operator docs**

Create `docs/operations/nexus-io-observability.md`:

```markdown
# Nexus I/O Observability

Issue #4062 adds a shared metrics surface for read, write, cache, ETag, prefetch, batching, coalescing, generation, and FUSE passthrough work.

## Scrape Targets

The FastAPI server exposes Python process metrics at `/metrics`.

The standalone `nexus-fuse` binary exposes FUSE process metrics when started with:

```bash
nexus-fuse mount /mnt/nexus --url http://localhost:2026 --api-key-file ./key --metrics-addr 127.0.0.1:9464
```

or:

```bash
NEXUS_FUSE_METRICS_ADDR=127.0.0.1:9464 nexus-fuse mount /mnt/nexus --url http://localhost:2026 --api-key-file ./key
```

Prometheus should scrape both targets when both processes are running.

## Metric Questions

| Metric | Question |
| --- | --- |
| `nexus_cache_requests_total{tier,result}` | Are reads hitting cache or falling through? |
| `nexus_cache_hit_ratio{tier}` | Is each cache tier healthy when native cache stats are available? |
| `nexus_cache_evictions_total{tier,reason}` | Why is cache capacity turning over? |
| `nexus_cache_bytes_in_use{tier}` | How full is the cache? |
| `nexus_cache_admission_rejected_total` | Is the admission filter protecting the disk tier? |
| `nexus_cache_etag_revalidate_total{result}` | Are stale cache entries cheaply revalidating? |
| `nexus_prefetch_issued_bytes_total` | How much data did prefetch request? |
| `nexus_prefetch_used_bytes_total` | How much prefetched data was consumed? |
| `nexus_prefetch_wasted_bytes_total` | How much prefetched data was evicted before use? |
| `nexus_prefetch_window_size{mount,workspace}` | What bounded-scope prefetch window is active? |
| `nexus_prefetch_pattern_detected_total{pattern}` | What access patterns are being detected? |
| `nexus_read_latency_seconds{tier}` | Where is read latency spent? |
| `nexus_read_bytes_total{tier}` | How much data is served by each tier? |
| `nexus_read_batch_size` | Are callers using batch reads effectively? |
| `nexus_fuse_passthrough_used_total` | Is large-read passthrough active? |
| `nexus_write_coalesce_flush_total{trigger}` | Why are buffered writes flushing? |
| `nexus_write_coalesce_dirty_bytes` | How many bytes are at durability risk? |
| `nexus_write_backend_rpc_total` | Did coalescing reduce backend write calls? |
| `nexus_generation_mismatch_total` | Are cached entries invalidated by generation drift? |
| `nexus_etag_check_total{result}` | Are ETag checks succeeding, updating, or failing? |

## Label Discipline

Labels are intentionally bounded. Paths, content IDs, ETags, user IDs, and raw file handles are not Prometheus labels. Use structured logs for per-path or per-handle debugging.

`nexus_prefetch_window_size` uses bounded `mount` and `workspace` labels. Until future prefetch work provides bounded operational scopes, these labels default to `default`.

## Current Sources

The foundation slice emits real data for:

- Python read latency and bytes from `NexusFSContent.sys_read`.
- Python batch size and batch bytes from `NexusFSContent.read_batch`.
- Python backend write RPC count from `NexusFSContent.sys_write` and `_write_locked`.
- FUSE SQLite cache hit, miss, stale, and bytes-in-use metrics.
- FUSE ETag revalidation and ETag check metrics.
- FUSE read latency, read bytes, and backend write RPC count.

Prefetch, coalescing, generation mismatch, foyer native stats, and passthrough metrics remain zero until their feature call sites land.
```

- [ ] **Step 4: Write Grafana dashboard JSON**

Create `observability/grafana/provisioning/dashboards/nexus-io-observability.json` with this compact dashboard:

```json
{
  "uid": "nexus-io-observability",
  "title": "Nexus I/O Observability",
  "description": "Read, write, cache, ETag, prefetch, coalescing, and passthrough metrics for issue #4062.",
  "tags": ["nexus", "io", "observability"],
  "timezone": "browser",
  "editable": true,
  "schemaVersion": 39,
  "version": 1,
  "refresh": "30s",
  "time": {
    "from": "now-1h",
    "to": "now"
  },
  "templating": {
    "list": [
      {
        "name": "datasource",
        "type": "datasource",
        "query": "prometheus",
        "current": {
          "text": "Prometheus",
          "value": "prometheus"
        },
        "label": "Datasource"
      }
    ]
  },
  "panels": [
    {
      "type": "timeseries",
      "title": "Read p95 Latency By Tier",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "targets": [
        {
          "expr": "histogram_quantile(0.95, sum by (le, tier) (rate(nexus_read_latency_seconds_bucket[5m])))",
          "legendFormat": "p95 {{tier}}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "s" } }
    },
    {
      "type": "timeseries",
      "title": "Read Bytes By Tier",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "targets": [
        {
          "expr": "sum by (tier) (rate(nexus_read_bytes_total[5m]))",
          "legendFormat": "{{tier}}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "Bps" } }
    },
    {
      "type": "timeseries",
      "title": "Cache Requests",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "targets": [
        {
          "expr": "sum by (tier, result) (rate(nexus_cache_requests_total[5m]))",
          "legendFormat": "{{tier}} {{result}}"
        }
      ]
    },
    {
      "type": "timeseries",
      "title": "Derived Cache Hit Percentage",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "targets": [
        {
          "expr": "100 * sum by (tier) (rate(nexus_cache_requests_total{result=\"hit\"}[5m])) / clamp_min(sum by (tier) (rate(nexus_cache_requests_total[5m])), 1)",
          "legendFormat": "{{tier}}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "percent" } }
    },
    {
      "type": "timeseries",
      "title": "Cache Bytes In Use",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 0, "y": 16 },
      "targets": [
        {
          "expr": "nexus_cache_bytes_in_use",
          "legendFormat": "{{tier}}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "bytes" } }
    },
    {
      "type": "timeseries",
      "title": "ETag Checks",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 8, "y": 16 },
      "targets": [
        {
          "expr": "sum by (result) (rate(nexus_etag_check_total[5m]))",
          "legendFormat": "{{result}}"
        }
      ]
    },
    {
      "type": "heatmap",
      "title": "Read Batch Size",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 16, "y": 16 },
      "targets": [
        {
          "expr": "sum by (le) (rate(nexus_read_batch_size_bucket[5m]))",
          "legendFormat": "{{le}}"
        }
      ]
    },
    {
      "type": "timeseries",
      "title": "Backend Write RPC Rate",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 24 },
      "targets": [
        {
          "expr": "rate(nexus_write_backend_rpc_total[5m])",
          "legendFormat": "write RPC/s"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "ops" } }
    },
    {
      "type": "timeseries",
      "title": "Prefetch And Coalescing Reserved",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 24 },
      "targets": [
        {
          "expr": "rate(nexus_prefetch_used_bytes_total[5m])",
          "legendFormat": "prefetch used B/s"
        },
        {
          "expr": "sum by (trigger) (rate(nexus_write_coalesce_flush_total[5m]))",
          "legendFormat": "flush {{trigger}}"
        }
      ]
    }
  ]
}
```

- [ ] **Step 5: Run docs/dashboard tests**

Run:

```bash
pytest tests/unit/services/test_io_metrics_docs.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add docs/operations/nexus-io-observability.md observability/grafana/provisioning/dashboards/nexus-io-observability.json tests/unit/services/test_io_metrics_docs.py
git commit -m "docs: add nexus io observability guide"
```

## Task 6: Full Verification

**Files:**

- No new files.
- Validate all files changed by Tasks 1-5.

- [ ] **Step 1: Run Python metrics tests**

Run:

```bash
pytest tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py tests/unit/core/test_nexus_fs_content_metrics.py tests/unit/services/test_io_metrics_docs.py -v
```

Expected: PASS.

- [ ] **Step 2: Run FUSE metrics tests**

Run:

```bash
cargo test --manifest-path nexus-fuse/Cargo.toml cache
cargo test --manifest-path nexus-fuse/Cargo.toml --test metrics_test
```

Expected: PASS.

- [ ] **Step 3: Run formatting checks for touched languages**

Run:

```bash
ruff format src/nexus/lib/io_metrics.py tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py tests/unit/core/test_nexus_fs_content_metrics.py tests/unit/services/test_io_metrics_docs.py
cargo fmt --manifest-path nexus-fuse/Cargo.toml
```

Expected: commands exit 0.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected: no unstaged files after the task commits.

- [ ] **Step 5: Final commit if verification changed formatting**

If Step 3 changed files, run:

```bash
git add src/nexus/lib/io_metrics.py tests/unit/services/test_io_metrics.py tests/integration/services/test_io_metrics_endpoint.py tests/unit/core/test_nexus_fs_content_metrics.py tests/unit/services/test_io_metrics_docs.py nexus-fuse/src/metrics.rs nexus-fuse/src/lib.rs nexus-fuse/src/main.rs nexus-fuse/src/cache.rs nexus-fuse/src/fs.rs
git commit -m "chore: format io observability changes"
```

Expected: commit succeeds only when formatting changed files.
