# Nexus FUSE Performance Measurement Results

## Overview

This document captures actual performance measurements for the Rust FUSE daemon compared to Python baseline.

**Status:** Ready for measurement (benchmarks built in Task #18)

## Quick Performance Test

To measure actual performance:

```bash
# 1. Start server
uv run nexus serve --port 2026 --api-key sk-test-key-123 --auth-type static &

# 2. Run quick benchmark sample
./nexus-fuse/run-benchmarks.sh --quick

# 3. View results
open target/criterion/report/index.html
```

## Issue #4053 Foyer Cache Benchmark

This section records the cache-backend benchmark used to validate replacing the
old SQLite file-content cache with the foyer hybrid cache.

**Command:**
```bash
cd nexus-fuse && cargo bench --bench cache_backends
```

**Environment:**
- Date: 2026-05-08 19:17:41 PDT
- OS: Darwin KWN9VC2WN4 25.3.0 arm64
- Rust: rustc 1.95.0 (59807616e 2026-04-14)
- Foyer: 0.22.3

**Benchmark setup:**
- Warm reads: 32 MiB foyer DRAM tier, 256 MiB filesystem tier
- Agent churn trace: 192 objects, 32-object hot set, 64 KiB/object, 2 MiB foyer DRAM tier, 256 MiB filesystem tier
- SQLite baseline: benchmark-only in-memory table with the same path/content/ETag shape
- No live Nexus server is required

The p99 values below are computed from Criterion's per-sample operation times
(`sample.json` sample time divided by sample iterations).

| Workload | Foyer mean | SQLite mean | Mean delta | Foyer p99 sample/op | SQLite p99 sample/op | p99 delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Warm read, 1 KiB | 215.6 ns | 1.09 us | 80.1% faster | 222.1 ns | 1.16 us | 80.8% faster |
| Warm read, 10 KiB | 325.9 ns | 1.54 us | 78.8% faster | 355.3 ns | 1.77 us | 80.0% faster |
| Warm read, 100 KiB | 2.27 us | 8.27 us | 72.6% faster | 3.14 us | 9.90 us | 68.3% faster |
| Warm read, 1 MiB | 21.65 us | 85.55 us | 74.7% faster | 45.87 us | 119.59 us | 61.6% faster |
| Agent churn trace | 27.43 us | 7.28 us | 276.7% slower | 34.01 us | 8.39 us | 305.4% slower |

Acceptance criterion met: the warm-read cache hot path shows at least 61.6%
p99 read-latency reduction versus the SQLite baseline, exceeding the 30%
target. The churn trace intentionally exceeds the foyer DRAM hot set and is
kept as visibility into filesystem-tier behavior; it is not the passing
criterion for this run.

Existing SQLite cache files under the nexus-fuse cache root are dropped on
cache startup, including legacy sanitized URL names like `http___host_2026.db`
and hash names like `nexus_HASH.db`. New foyer cache content is stored under a
sibling `nexus_HASH.foyer/` directory.

## Expected vs Actual Performance

### Startup Latency

| Metric | Target | Actual | Notes |
|--------|--------|--------|-------|
| Daemon spawn | < 100ms | TBD | Rust binary startup + socket creation |
| Socket connect | < 10ms | TBD | Unix socket IPC handshake |
| First operation | < 50ms | TBD | Initial HTTP request + cache prime |

**Measurement command:**
```bash
time uv run python nexus-fuse/test_mount_integration.py
```

### Read/Write Latency (1KB files)

| Operation | Python Baseline | Rust Target | Actual | Speedup |
|-----------|----------------|-------------|--------|---------|
| read (cached) | ~10ms | ~0.1ms | TBD | TBD |
| read (cold) | ~50ms | ~5ms | TBD | TBD |
| write | ~100ms | ~10ms | TBD | TBD |

**Measurement command:**
```bash
cargo bench read_1kb
cargo bench write_1kb
```

### Directory Operations

| Operation | Python Baseline | Rust Target | Actual | Speedup |
|-----------|----------------|-------------|--------|---------|
| list (100 files) | ~200ms | ~20ms | TBD | TBD |
| stat | ~20ms | ~2ms | TBD | TBD |
| mkdir | ~50ms | ~5ms | TBD | TBD |

**Measurement command:**
```bash
cargo bench list
cargo bench stat
cargo bench mkdir
```

### File Management

| Operation | Python Baseline | Rust Target | Actual | Speedup |
|-----------|----------------|-------------|--------|---------|
| delete | ~50ms | ~5ms | TBD | TBD |
| rename | ~50ms | ~5ms | TBD | TBD |
| exists | ~20ms | ~2ms | TBD | TBD |

**Measurement command:**
```bash
cargo bench delete
cargo bench rename
cargo bench exists
```

## Throughput (operations/second)

| Operation | Python Baseline | Rust Target | Actual | Improvement |
|-----------|----------------|-------------|--------|-------------|
| Sequential reads | ~100 ops/s | ~10,000 ops/s | TBD | TBD |
| Sequential writes | ~10 ops/s | ~100 ops/s | TBD | TBD |
| Mixed workload | ~50 ops/s | ~500 ops/s | TBD | TBD |

**Measurement command:**
```bash
# Run full benchmark suite
./nexus-fuse/run-benchmarks.sh
```

## Memory Usage

| Metric | Python | Rust | Savings |
|--------|--------|------|---------|
| Daemon RSS | TBD | TBD | TBD |
| Cache size (100 files) | TBD | TBD | TBD |
| Per-connection overhead | TBD | TBD | TBD |

**Measurement command:**
```bash
# Monitor during benchmark run
ps aux | grep nexus-fuse
```

## Performance Factors

### Why Rust is Faster

1. **No GIL Contention**
   - Python: Single-threaded due to GIL
   - Rust: True multi-threading with tokio

2. **Native Async I/O**
   - Python: Blocking I/O with thread pools
   - Rust: Tokio async runtime (epoll/kqueue)

3. **Persistent Hybrid Cache**
   - Python: In-memory dict (process lifetime)
   - Rust: Foyer DRAM tier plus filesystem tier

4. **Zero-Copy Operations**
   - Python: Multiple object allocations per operation
   - Rust: Direct buffer operations, minimal allocations

5. **Compiled Code**
   - Python: Interpreted bytecode
   - Rust: Native machine code with LLVM optimizations

### Performance Degradation Scenarios

These scenarios may NOT see significant speedup:

1. **Context-Aware Operations**
   - Falls back to Python for permission checks
   - Namespace-scoped mounts use Python

2. **Virtual Views**
   - Parsed file content (`.md`, `.txt`) uses Python
   - View transformations require Python logic

3. **Network-Bound Operations**
   - When Nexus server is slow, client speed less important
   - Network latency dominates computation time

4. **First-Time Operations**
   - Cache warmup requires actual backend calls
   - No speedup until cache is populated

## Measurement Methodology

### Python Baseline

```bash
# Time full test suite
time uv run python nexus-fuse/test_mount_integration.py

# Extract individual operation times from logs
grep "✓" output.log | awk '{print $NF}'
```

### Rust Benchmarks

```bash
# Run full benchmark suite
cargo bench --bench fuse_operations

# Extract specific operation results
cargo bench read_1kb_cached -- --output-format bencher

# Compare with saved baseline
cargo bench -- --baseline python-equivalent
```

### Statistical Significance

- **Sample size**: 100 iterations (Criterion default)
- **Measurement time**: 10 seconds per benchmark
- **Confidence interval**: 95% (Criterion default)
- **Outlier detection**: Enabled (reject >5% deviation)

## CI/CD Performance Regression Detection

```yaml
# .github/workflows/perf.yml
- name: Run performance tests
  run: ./nexus-fuse/run-benchmarks.sh

- name: Compare with baseline
  run: |
    cargo bench -- --baseline main --save-baseline pr

- name: Fail on regression
  run: |
    # Fail if any operation is >10% slower
    cargo bench -- --baseline main | grep "Performance has regressed"
```

## How to Update This Document

After running benchmarks:

1. Run benchmarks: `./nexus-fuse/run-benchmarks.sh`
2. Open Criterion report: `open target/criterion/report/index.html`
3. Extract median times from report
4. Update "Actual" columns in tables above
5. Calculate speedup: `Python time / Rust time`
6. Add notes about unexpected results

## References

- [Task #18: Benchmark Implementation](TASK18_COMPLETE.md)
- [Task #17: Rust Integration](TASK17_COMPLETE.md)
- [Method Delegation Status](METHOD_DELEGATION_STATUS.md)
- [Criterion.rs Book](https://bheisler.github.io/criterion.rs/book/)

---

**Status:** 🟡 Ready for measurement (benchmarks built, pending actual run)

## 2026-05-09 — Issue #4055 Hydration Benchmark

Setup: mockito local server, 50 small files of 256 bytes each, criterion `iter_custom` measuring wall time for sequential 50-file reads.

| Scenario | Median wall time | Notes |
|---|---|---|
| `cold_read_p50_no_hydration` | 90.1 ms | Cache cold, each read goes to mockito (~0 RTT on localhost) |
| `cold_read_p50_with_hydration` | 27.4 µs | Hydrate ran first (parallel admit, concurrency=8), then reads from foyer DRAM |

Speedup ratio under mockito: **~3,284x**.

Caveat: mockito has near-zero RTT, so the cold-cache scenario doesn't pay realistic network costs — its 90 ms is dominated by foyer's per-cache-open initialization overhead (new temp dir per iteration), not network latency. On a production backend with real network latency, the cold-read path would be even slower (serial HTTP round-trips at 10–100 ms each = 500 ms–5 s for 50 files), while hydration uses bounded-parallel fetches (concurrency=8) so its wall time scales much better. The ratio on a real backend is expected to be well above 3×.

**Environment:**
- Date: 2026-05-09
- OS: Darwin 25.3.0 arm64
- Rust: rustc 1.95.0
- Foyer: 0.22.3
- Criterion: 0.8, sample_size=20, measurement_time=8s

**Next Step:** Run `./nexus-fuse/run-benchmarks.sh` to populate actual results

## 2026-05-10 — Issue #4056 Concurrent-Read Throughput

Validates the migration from `reqwest::blocking` to async hyper/reqwest with
a shared connection pool. Acceptance criterion was ≥2× concurrent-read
throughput vs. the pre-PR path.

**Command:**
```bash
cd nexus-fuse && cargo bench --bench concurrent_read
```

**Setup:** Local multi-thread tokio HTTP/1.1 responder bound to 127.0.0.1.
Mockito was rejected for this comparison because it runs a `current_thread`
runtime that serializes accepts — that masks any client-side concurrency
win. The bench server returns a fixed JSON-RPC payload with
`Connection: keep-alive` so the pooled client can reuse sockets.

- **pooled**: one `NexusClient` shared across N reader threads (post-#4056).
- **unpooled**: each read builds a fresh `NexusClient`, emulating the
  worst-case behavior the issue describes (no pool reuse, fresh handshake
  per request).

Unpooled is intentionally bounded (8 ops/thread) because every iteration
leaves a socket in TIME_WAIT and macOS's ephemeral-port range is small;
pooled runs 256 ops/thread.

| Threads | pooled ops/s | unpooled ops/s | speedup |
|---|---|---|---|
| 1  |  6 090 | 3 532 | 1.72× |
| 4  | 30 576 | 7 900 | 3.87× |
| 8  | 38 215 | 9 088 | 4.21× |
| 16 | 41 684 | 7 860 | 5.30× |

**Acceptance:** ≥2× at every concurrent (≥4-thread) setting; 5.30× at the
high end. Single-thread is below 2× because the unpooled path still
benefits from the static HTTP runtime warm-up and reqwest's intra-call
pool warm — the win only opens up once concurrency starts amortizing the
saved handshakes across threads, which is exactly the FUSE multi-worker
use case the issue targets.

**Environment:**
- Date: 2026-05-10
- OS: Darwin 25.3.0 arm64
- Rust: rustc 1.95.0
- reqwest: 0.13 (async, rustls)
- tokio: 1 (multi-thread, 2 worker threads in the shared HTTP runtime)
