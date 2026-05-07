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
    {
        "backend",
        "virtual",
        "error",
        "batch",
        "cache",
        "sqlite",
        "dram",
        "nvme",
        "passthrough",
        "other",
    }
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


def record_read_bytes(*, tier: str, bytes_read: int) -> None:
    READ_BYTES.labels(tier=_bounded(tier, _READ_TIERS)).inc(_nonnegative(bytes_read))


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
