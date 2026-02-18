# Nexus FUSE Performance Benchmarks

Comprehensive benchmarks for Nexus FUSE operations using Criterion.rs.

## Prerequisites

**IMPORTANT:** The Nexus server must be running before executing benchmarks.

```bash
# Start the test server (in a separate terminal)
uv run nexus serve --port 2026 --api-key sk-test-key-123 --auth-type static
```

## Running Benchmarks

### Run All Benchmarks
```bash
cargo bench
```

### Run Specific Benchmark
```bash
# Read operations only
cargo bench read

# Write operations only
cargo bench write

# Directory listing
cargo bench list

# All stat/metadata operations
cargo bench stat
```

### Run with Additional Options
```bash
# Save baseline for comparison
cargo bench --bench fuse_operations -- --save-baseline rust-v1

# Compare against baseline
cargo bench --bench fuse_operations -- --baseline rust-v1

# Shorter runs (for quick iteration)
cargo bench --bench fuse_operations -- --quick

# Verbose output
cargo bench --bench fuse_operations -- --verbose
```

## Benchmark Coverage

### Read Operations
- **read_1kb_cached** - Cached file reads (warm cache)
- **read_1kb_cold** - Cold file reads (cache miss)
- **read_by_size** - Read performance across sizes (1KB, 10KB, 100KB, 1MB)

### Write Operations
- **write_1kb** - Small file writes (1KB)
- **write_by_size** - Write performance across sizes (1KB, 10KB, 100KB, 1MB)

### Directory Operations
- **list_100_files** - List directory with 100 files
- **stat** - File metadata retrieval

### File Management
- **mkdir** - Directory creation
- **delete** - File deletion
- **rename** - File rename/move
- **exists_true** - Check for existing file
- **exists_false** - Check for non-existent file

## Interpreting Results

### Sample Output
```
read_1kb_cached         time:   [1.2345 ms 1.2567 ms 1.2789 ms]
                        change: [-5.1234% -3.4567% -1.7890%] (p = 0.00 < 0.05)
                        Performance has improved.
```

### Understanding Metrics
- **time**: Median execution time with 95% confidence interval
- **change**: Performance delta vs previous run (if baseline exists)
- **p-value**: Statistical significance (p < 0.05 = significant)

### Performance Targets

Based on Python baseline (estimated):

| Operation | Python | Rust Target | Speedup |
|-----------|--------|-------------|---------|
| read (cached) | ~10ms | **~0.1ms** | 100x |
| read (cold) | ~50ms | **~5ms** | 10x |
| write | ~100ms | **~10ms** | 10x |
| readdir | ~200ms | **~20ms** | 10x |
| stat | ~20ms | **~2ms** | 10x |
| mkdir | ~50ms | **~5ms** | 10x |
| delete | ~50ms | **~5ms** | 10x |
| rename | ~50ms | **~5ms** | 10x |

## Advanced Usage

### Generate Performance Report
```bash
# Generate HTML report
cargo bench --bench fuse_operations

# Open report in browser
open target/criterion/report/index.html
```

### Profile with Flamegraph
```bash
# Install cargo-flamegraph
cargo install flamegraph

# Profile specific benchmark
cargo flamegraph --bench fuse_operations -- --bench read_1kb_cached
```

### Compare Python vs Rust

To compare Python vs Rust performance:

1. **Run Python baseline:**
   ```bash
   # Use test_ipc_standalone.py or mount with Python-only mode
   time python nexus-fuse/test_ipc_standalone.py
   ```

2. **Run Rust benchmarks:**
   ```bash
   cargo bench --bench fuse_operations
   ```

3. **Analyze results:**
   - Python: Wall-clock time from `time` command
   - Rust: Criterion median time from benchmark output
   - Calculate speedup: Python time / Rust time

## Benchmark Configuration

Current settings (in `benches/fuse_operations.rs`):
- **Measurement time**: 10 seconds per benchmark
- **Sample size**: 100 iterations
- **Warm-up time**: 3 seconds (Criterion default)

To modify:
```rust
config = Criterion::default()
    .measurement_time(Duration::from_secs(10))
    .sample_size(100)
    .warm_up_time(Duration::from_secs(3));
```

## Troubleshooting

### Error: Connection Refused
**Problem:** Nexus server is not running

**Solution:**
```bash
uv run nexus serve --port 2026 --api-key sk-test-key-123 --auth-type static
```

### Error: Permission Denied
**Problem:** API key mismatch

**Solution:** Ensure benchmark uses `sk-test-key-123` matching server

### Inconsistent Results
**Problem:** System load affecting benchmarks

**Solutions:**
- Close other applications
- Disable CPU frequency scaling
- Run multiple times and compare
- Use `--save-baseline` to track trends

## CI/CD Integration

For automated performance regression detection:

```yaml
# .github/workflows/bench.yml
- name: Run benchmarks
  run: |
    uv run nexus serve --port 2026 --api-key sk-test-key-123 --auth-type static &
    sleep 2
    cargo bench --bench fuse_operations -- --save-baseline ci

- name: Compare with main
  run: |
    git checkout main
    cargo bench --bench fuse_operations -- --save-baseline main
    cargo bench --bench fuse_operations -- --baseline main
```

## References

- [Criterion.rs Documentation](https://bheisler.github.io/criterion.rs/book/)
- [Rust Performance Book](https://nnethercote.github.io/perf-book/)
- [METHOD_DELEGATION_STATUS.md](../METHOD_DELEGATION_STATUS.md) - Expected performance gains
