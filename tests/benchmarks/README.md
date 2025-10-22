# Nexus Performance Benchmarks

This directory contains performance benchmarks for the Nexus filesystem.

## Overview

The benchmark suite tests Nexus performance against raw filesystem operations to measure:
- Write/read throughput for various file sizes
- CAS deduplication efficiency
- Cache hit rates and effectiveness
- Multi-agent concurrency performance

## Running Benchmarks

### Run all benchmarks
```bash
pytest tests/benchmarks/ --benchmark-only
```

### Run specific benchmark group
```bash
# Write throughput
pytest tests/benchmarks/test_throughput.py::TestWriteThroughput --benchmark-only

# Deduplication
pytest tests/benchmarks/test_dedup.py --benchmark-only

# Cache performance
pytest tests/benchmarks/test_cache.py --benchmark-only

# Concurrency
pytest tests/benchmarks/test_concurrency.py --benchmark-only
```

### Compare embedded vs local filesystem
```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-compare --benchmark-group-by=param:backend_type
```

### Run with verbose output
```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-verbose
```

### Save baseline
```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline
```

### Compare to baseline
```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-compare=baseline
```

### Generate comparison report
```bash
# Save results from different runs
pytest tests/benchmarks/ --benchmark-only --benchmark-save=nexus-v1
# After changes
pytest tests/benchmarks/ --benchmark-only --benchmark-save=nexus-v2
# Compare
pytest-benchmark compare nexus-v1 nexus-v2
```

## Benchmark Groups

### 1. Write Throughput (`test_throughput.py::TestWriteThroughput`)
- Tests write performance for 1KB, 100KB, 1MB, and 10MB files
- Compares Nexus (with CAS) vs raw filesystem writes

### 2. Read Throughput (`test_throughput.py::TestReadThroughput`)
- Tests read performance for various file sizes
- Measures cache effectiveness

### 3. Small Files (`test_throughput.py::TestSmallFileThroughput`)
- Tests performance with many small files (common in AI workloads)
- Measures metadata overhead

### 4. Deduplication (`test_dedup.py`)
- Measures CAS deduplication efficiency
- Tests storage savings when writing duplicate content
- Compares deduplicated vs non-deduplicated storage

### 5. Cache (`test_cache.py`)
- Tests cold vs warm cache performance
- Measures cache hit rates
- Tests various access patterns

### 6. Concurrency (`test_concurrency.py`)
- Tests multi-agent concurrent writes
- Tests concurrent reads
- Tests mixed read/write workloads
- Stress tests with 50+ concurrent operations

## Interpreting Results

### Metrics
- **Min/Max**: Fastest and slowest execution times
- **Mean**: Average execution time
- **StdDev**: Standard deviation (lower = more consistent)
- **Median**: Middle value (50th percentile)
- **IQR**: Interquartile range (25th to 75th percentile)
- **Ops/s**: Operations per second (higher = better)

### Expected Performance

#### Embedded Nexus vs Local FS
- **Writes**: Nexus may be slightly slower due to metadata + CAS overhead
- **Reads**: Nexus should be competitive, potentially faster with caching
- **Deduplication**: Nexus should show significant storage savings
- **Concurrency**: Nexus should handle concurrent access well

#### Performance Targets
- Write throughput: >50 MB/s for large files
- Read throughput: >100 MB/s for cached files
- Deduplication: >80% storage savings for duplicate content
- Concurrency: <2x overhead for 10 concurrent agents

## CI Integration

These benchmarks can be integrated into CI to detect performance regressions:

```yaml
# .github/workflows/benchmark.yml
- name: Run benchmarks
  run: pytest tests/benchmarks/ --benchmark-only --benchmark-json=output.json

- name: Compare with baseline
  run: pytest-benchmark compare baseline output.json

- name: Alert on regression
  run: pytest-benchmark compare --fail-on-regression=10%
```

## Notes

- Benchmarks use `pytest-benchmark` which automatically handles warmup, iterations, and statistics
- Each backend type (embedded, local_fs) is tested with the same operations for fair comparison
- Tests are isolated using temporary directories
- Some tests like deduplication are Nexus-specific and demonstrate unique capabilities
