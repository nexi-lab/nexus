# Nexus Performance Benchmarks

Comprehensive performance tests comparing Nexus against native filesystem operations.

## Quick Start

```bash
cd benchmarks/performance
./run_benchmark.sh
```

The script will automatically:
- Start a fresh PostgreSQL database for benchmarks
- Generate test data if missing (~250MB in `/tmp/nexus_perf_data`)
- Initialize a Nexus server
- Run all benchmarks
- Generate HTML and PDF reports in `result_YYYYMMDD_HHMMSS/`

## Benchmark Tests

1. **Flat Directory** - 1K and 10K files in single directory
   - List, read, and stat operations
   - Tests: Native Bash, Native Python, Nexus, Docker Sandbox (FUSE)

2. **Grep Operations** - Pattern search across files
   - Medium-sized files (~13KB each)
   - Tests: Native Bash, Native Python, Nexus, Docker Sandbox (Bash/Python)

3. **Nested Directory** - 10K files in nested structure
   - Recursive operations
   - Tests: Native Bash, Native Python, Nexus, Docker Sandbox (FUSE)

## Requirements

- **Docker** - Must be running (for PostgreSQL and sandbox tests)
- **Python 3.11+** - With nexus package installed
- **Disk Space** - ~250MB for test data + ~1GB for results
- **Time** - ~10-15 minutes for full suite

## Results

Results are saved in timestamped directories:
- `result_YYYYMMDD_HHMMSS/performance_summary.html` - Main report with charts
- `result_YYYYMMDD_HHMMSS/performance_summary.pdf` - PDF version
- `*_comparison_results.csv` - Raw data for each test
- `*_test.log` - Detailed test logs

## Test Data

Test data is generated in `/tmp/nexus_perf_data/`:
- `flat_50k/` - 50K files for flat directory tests
- `nested_50k/` - 50K files in nested structure
- `grep_medium_1k/` - 1K files for grep tests
- `grep_medium_10k/` - 10K files for grep tests
- Additional datasets for specialized tests

To regenerate test data:
```bash
python3 generate_perf_data.py
```

## Customization

Edit `run_benchmark.sh` to:
- Change timeout values (default: 180s for sandbox tests)
- Enable/disable specific tests
- Modify database connection settings
