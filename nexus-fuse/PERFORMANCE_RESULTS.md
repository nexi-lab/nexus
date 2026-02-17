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

3. **Persistent Cache**
   - Python: In-memory dict (process lifetime)
   - Rust: SQLite (persistent across restarts)

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

**Next Step:** Run `./nexus-fuse/run-benchmarks.sh` to populate actual results
