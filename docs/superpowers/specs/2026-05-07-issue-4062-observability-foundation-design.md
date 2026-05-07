# Issue 4062 Observability Foundation Design

**Issue:** [#4062 - Observability: cache, prefetch, and read-path metrics](https://github.com/nexi-lab/nexus/issues/4062)

## Summary

Issue #4062 asks for a shared metrics surface before the cache, prefetch, batching, write-coalescing, and passthrough work lands. The full issue references future features tracked separately in #4053, #4057, #4058, #4059, and #4060, so this design implements the foundation slice:

1. Define the Prometheus metric catalog now.
2. Wire metrics to current read, write, batch, SQLite cache, and ETag paths.
3. Provide stable recorder APIs for future foyer, prefetch, coalescing, and passthrough call sites.
4. Commit a standard Grafana dashboard and metric-to-question documentation.

This slice intentionally does not implement foyer, prefetch, write coalescing, generation numbers, or FUSE passthrough. It makes those follow-up PRs measurable when they land.

## Current Context

The repo already has Python-side Prometheus infrastructure in `src/nexus/server/metrics.py`. That module registers HTTP request metrics and exposes `/metrics` through the existing FastAPI app. Activity metrics follow the same global `prometheus_client.REGISTRY` pattern in `src/nexus/services/activity/metrics.py`, with tests that scrape `generate_latest()`.

The current core read/write hot path crosses through `src/nexus/core/nexus_fs_content.py` into `rust/kernel/src/kernel/io.rs`. The Rust kernel owns routing, permission checks, VFS locks, backend reads/writes, metadata updates, and mutation observers. Python still wraps the public API and is the natural low-risk place to record the current metrics without introducing Prometheus dependencies into the Rust kernel crate.

The standalone `nexus-fuse` crate is excluded from the root Cargo workspace. It has its own SQLite-backed file cache in `nexus-fuse/src/cache.rs`, ETag revalidation in `nexus-fuse/src/fs.rs`, and no existing metrics endpoint. The current cache is not foyer yet; this foundation records the current SQLite behavior using the same tier/result names that the future foyer integration will reuse.

## Non-Goals

This design does not:

- Replace SQLite with foyer (#4053).
- Add file generations (#4054).
- Implement the adaptive prefetch crate (#4057).
- Implement `_read_batch()` beyond existing behavior (#4058).
- Add write coalescing (#4059).
- Add FUSE passthrough (#4060).
- Add raw file paths, handles, workspace IDs, user IDs, or cache keys as Prometheus labels.

## Architecture

### Python Prometheus Catalog

Create `src/nexus/lib/io_metrics.py` as the shared read/write/cache metrics catalog for the server process. It will mirror the existing activity metrics style: module-level `Counter`, `Gauge`, and `Histogram` objects registered on the global Prometheus registry at import time, plus small `record_*` functions used by call sites.

The catalog is intentionally low-cardinality. Prometheus labels are bounded strings such as `tier`, `result`, `trigger`, `pattern`, `operation`, `mount`, and `workspace`. The `mount` and `workspace` labels are normalized to `"default"` unless the call site can provide a bounded operational name. Raw file handles, paths, content IDs, etags, and user IDs are never Prometheus labels.

### Server Read/Write Instrumentation

Instrument `src/nexus/core/nexus_fs_content.py` around public file I/O methods:

- `sys_read()`: observe `nexus_read_latency_seconds{tier}` and increment `nexus_read_bytes_total{tier}`. Current tier mapping:
  - `backend` for normal kernel reads that return data.
  - `virtual` for resolver-handled reads.
  - `error` for failed reads.
- `read_batch()`: observe `nexus_read_batch_size` with the requested path count. Record per-result bytes under tier `batch`.
- `sys_write()` and `_write_locked()`: increment `nexus_write_backend_rpc_total` only when `Kernel.sys_write()` returns `hit=True`, because that indicates Rust performed the backend write path. Future write coalescing will move this counter to the actual flush call site.

This keeps the first slice close to the current Python API boundary and avoids deep Rust refactors while still measuring the operational surface users exercise.

### FUSE Metrics

Add `nexus-fuse/src/metrics.rs` with thread-safe counters/histograms backed by atomics and bucket arrays. The crate will expose a Prometheus text endpoint when explicitly enabled by `--metrics-addr` or `NEXUS_FUSE_METRICS_ADDR`. This avoids changing default daemon behavior.

Current FUSE instrumentation:

- `FileCache::get()` records `nexus_cache_requests_total{tier="sqlite", result="hit|miss|stale"}`.
- `FileCache::put()` and `FileCache::stats()` update `nexus_cache_bytes_in_use{tier="sqlite"}`.
- `NexusFs::read_cached()` records `nexus_cache_etag_revalidate_total{result="304|updated|error|fallback"}` and `nexus_etag_check_total{result=...}`.
- `NexusFs::read()` records read latency and bytes with tiers `cache`, `backend`, or `error`.
- `NexusFs::write()` records `nexus_write_backend_rpc_total` for each client write request that reaches the backend.

The FUSE endpoint will expose the same metric names as the Python server. Operators can scrape either or both processes depending on deployment topology.

### Future Recorder Contract

The foundation module exposes recorders for both current and future features. Recorders tied to future features register their collectors now and update real series only when those feature call sites land:

- `record_cache_request(tier, result)`
- `set_cache_hit_ratio(tier, ratio)`
- `record_cache_eviction(tier, reason)`
- `set_cache_bytes_in_use(tier, bytes)`
- `record_cache_admission_rejected()`
- `record_etag_check(result)`
- `record_prefetch_issued(bytes)`
- `record_prefetch_used(bytes)`
- `record_prefetch_wasted(bytes)`
- `set_prefetch_window_size(window_size, mount="default", workspace="default")`
- `record_prefetch_pattern(pattern)`
- `record_read(tier, bytes_read, latency_seconds)`
- `record_read_batch_size(count)`
- `record_fuse_passthrough_used()`
- `record_write_coalesce_flush(trigger)`
- `set_write_coalesce_dirty_bytes(bytes)`
- `record_write_backend_rpc()`
- `record_generation_mismatch()`

Future PRs should call these recorders rather than defining new metric objects. That keeps metric names stable and prevents duplicate collectors.

## Metric Catalog

### Cache

| Metric | Type | Labels | Current Source | Question |
| --- | --- | --- | --- | --- |
| `nexus_cache_requests_total` | Counter | `tier`, `result` | `nexus-fuse` SQLite cache | Are reads hitting cache or falling through? |
| `nexus_cache_hit_ratio` | Gauge | `tier` | Future foyer stats; current dashboard derives hit percentage from `nexus_cache_requests_total` | Is each cache tier healthy? |
| `nexus_cache_evictions_total` | Counter | `tier`, `reason` | Future foyer stats | Why is cache capacity turning over? |
| `nexus_cache_bytes_in_use` | Gauge | `tier` | SQLite stats now; foyer stats later | How full is the cache? |
| `nexus_cache_admission_rejected_total` | Counter | none | Future foyer admission filter | Is TinyLFU protecting the disk tier? |
| `nexus_cache_etag_revalidate_total` | Counter | `result` | `read_cached()` | Are stale entries cheaply revalidating? |

### Prefetch

| Metric | Type | Labels | Current Source | Question |
| --- | --- | --- | --- | --- |
| `nexus_prefetch_issued_bytes_total` | Counter | none | Future prefetch crate | How much data did prefetch request? |
| `nexus_prefetch_used_bytes_total` | Counter | none | Future prefetch crate | How much prefetched data was consumed? |
| `nexus_prefetch_wasted_bytes_total` | Counter | none | Future prefetch crate | How much prefetch work was wasted? |
| `nexus_prefetch_window_size` | Gauge | `mount`, `workspace` | Future prefetch crate | What adaptive window is active for bounded scopes? |
| `nexus_prefetch_pattern_detected_total` | Counter | `pattern` | Future prefetch crate | What access patterns are being detected? |

`file_handle` is deliberately not a Prometheus label. It is high-cardinality and short-lived. Future prefetch code should log per-handle values as structured log fields when debugging is enabled.

### Read Path

| Metric | Type | Labels | Current Source | Question |
| --- | --- | --- | --- | --- |
| `nexus_read_latency_seconds` | Histogram | `tier` | Python server and `nexus-fuse` | Where is read latency spent? |
| `nexus_read_bytes_total` | Counter | `tier` | Python server and `nexus-fuse` | How much data is served by each tier? |
| `nexus_read_batch_size` | Histogram | none | `read_batch()` | Are clients using batch reads effectively? |
| `nexus_fuse_passthrough_used_total` | Counter | none | Future passthrough call site | Is large-read passthrough active? |

Histogram buckets use sub-millisecond through 10-second coverage:

`0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10`

Batch size buckets:

`1, 2, 4, 8, 16, 32, 64, 128, 256, 512`

### Write Path

| Metric | Type | Labels | Current Source | Question |
| --- | --- | --- | --- | --- |
| `nexus_write_coalesce_flush_total` | Counter | `trigger` | Future coalescing buffer | Why are buffered writes flushing? |
| `nexus_write_coalesce_dirty_bytes` | Gauge | none | Future coalescing buffer | How many bytes are at durability risk? |
| `nexus_write_backend_rpc_total` | Counter | none | Python server and `nexus-fuse` | Did coalescing reduce backend write calls? |

### Consistency

| Metric | Type | Labels | Current Source | Question |
| --- | --- | --- | --- | --- |
| `nexus_generation_mismatch_total` | Counter | none | Future generation-aware cache invalidation | Are cached entries invalidated by generation drift? |
| `nexus_etag_check_total` | Counter | `result` | Current ETag revalidation path | Are ETag checks succeeding, updating, or failing? |

## Structured Logs

Prometheus answers aggregate questions. Slow-path and high-cardinality details go to structured logs:

- Cache miss to backend: `event="nexus.cache_miss_backend"`, `tier`, `path_hash`, `bytes`, `latency_ms`.
- ETag revalidation: `event="nexus.etag_revalidate"`, `result`, `status`, `path_hash`.
- Future prefetch reset: `event="nexus.prefetch_reset"`, `file_handle`, `pattern`, `old_window`, `reason`.
- Future write flush: `event="nexus.write_coalesce_flush"`, `trigger`, `dirty_bytes`, `path_hash`.

`path_hash` is a stable hash of the path for correlation without leaking raw paths into metrics or logs by default.

## Dashboard

Commit `observability/grafana/provisioning/dashboards/nexus-io-observability.json` with panels for:

1. Read p50/p95/p99 latency by tier.
2. Read bytes by tier.
3. Cache requests by tier/result.
4. Cache hit ratio by tier.
5. Cache bytes in use by tier.
6. ETag checks by result.
7. Read batch size distribution.
8. Write backend RPC rate.
9. Reserved panels for prefetch efficiency and coalescing flush triggers, showing zero data until those feature metrics are emitted.

The existing `observability/grafana/provisioning/dashboards/dashboards.yml` already provisions dashboards from this directory, so no provisioning change is needed unless the current provider is path-specific.

## Testing

Python tests:

- Add unit tests for every `io_metrics` recorder to prove the metric is registered and updates the expected sample.
- Add targeted tests around `NexusFSContent.sys_read()`, `read_batch()`, and `_write_locked()` using lightweight fakes/mocks for the Rust kernel boundary.
- Add an integration-style scrape test using `prometheus_client.generate_latest()` to assert the catalog appears in `/metrics` output after recorder calls.

Rust FUSE tests:

- Add unit tests for the metrics encoder in `nexus-fuse/src/metrics.rs`.
- Add cache tests asserting `FileCache::get()` records hit, miss, and stale outcomes.
- Add `read_cached()` tests for ETag result counters where existing mock client infrastructure allows it.

Manual verification:

```bash
pytest tests/unit/services/test_io_metrics.py -v
pytest tests/integration/services/test_io_metrics_endpoint.py -v
cargo test --manifest-path nexus-fuse/Cargo.toml metrics cache
```

## Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Duplicate collectors during tests | Follow activity metrics patterns and avoid dynamic collector creation in tests. |
| Cardinality blow-up | Keep labels bounded; raw handles and paths stay out of Prometheus. |
| Rust/Python metric mismatch | Use identical metric names and document that server and FUSE are separate scrape targets. |
| Foundation drifts from future foyer/prefetch work | Expose recorder APIs now and reference them from #4053/#4057/#4059/#4060 implementation plans. |
| Metrics overhead on hot paths | Record simple counter/histogram observations only; no path hashing unless a structured slow-path log is emitted. |

## Acceptance Criteria Mapping

- All issue-named metrics are defined and exposed through the Python `/metrics` endpoint after import and through the `nexus-fuse` metrics endpoint when the daemon is started with metrics enabled.
- Current read/write/cache/ETag paths update the metrics that have real sources today.
- Future metrics have stable recorders and reserved dashboard panels documented as zero until their feature lands.
- Histograms include sub-millisecond through 10-second buckets.
- Prometheus labels are bounded; `mount` and `workspace` default to `"default"` until reliable bounded values are available.
- Dashboard JSON is committed under `observability/grafana/provisioning/dashboards/`.
- Documentation explains which metric answers which operational question.
