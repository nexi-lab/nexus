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

The same `--metrics-addr` flag and `NEXUS_FUSE_METRICS_ADDR` environment variable also work for the `daemon` subcommand.

For the repository Docker observability stack, scrape the API through the Compose service DNS name `nexus-server:2026`. Use `localhost:2026` only when Prometheus runs on the host next to a host-run Nexus API process.

For containerized Prometheus deployments, 127.0.0.1:9464 must be reachable from the Prometheus process or container. If Prometheus runs outside the FUSE process namespace, bind the FUSE metrics listener to a reachable interface and scrape that address instead.

Example scrape jobs:

```yaml
scrape_configs:
  - job_name: nexus-api
    metrics_path: /metrics
    static_configs:
      - targets: ["nexus-server:2026"]

  - job_name: nexus-fuse
    static_configs:
      - targets: ["127.0.0.1:9464"]
```

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

Prefetch, coalescing, generation mismatch, foyer native stats, and passthrough metrics remain zero or absent until their labeled series are first observed or their feature call sites land.
