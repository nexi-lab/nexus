---
name: Performance Issue
about: Report a performance problem or optimization opportunity
title: '[PERF] '
labels: ['performance']
assignees: ''
---

## Performance Issue

**What operation is slow or resource-intensive?**

Describe the performance problem you're experiencing.

## Performance Metrics

**Current Performance:**

- **Operation:** [e.g., file read, semantic search, write batch]
- **Duration:** [e.g., 5 seconds, 2 minutes]
- **Throughput:** [e.g., 10 MB/s, 100 ops/sec]
- **Resource Usage:** [e.g., CPU 80%, Memory 4GB]

**Expected Performance:**

Based on the [performance targets](https://github.com/nexi-lab/nexus/blob/main/README.md#performance-targets), what performance did you expect?

## Reproduction Steps

1. Setup: Describe your configuration
2. Action: What operation did you perform?
3. Measurement: How did you measure performance?

**Reproducible Code:**

```python
import nexus
import time

# Code to reproduce the performance issue
start = time.time()
# Your code here
duration = time.time() - start
print(f"Operation took {duration:.2f} seconds")
```

## Environment

- **Nexus Version:** [e.g., 0.1.0]
- **Python Version:** [e.g., 3.11.5]
- **OS:** [e.g., Ubuntu 22.04]
- **Deployment Mode:** [embedded/monolithic/distributed]
- **Hardware:** [e.g., 4 CPU cores, 16GB RAM, SSD]

**Workload Details:**

- **Data Size:** [e.g., 1000 files, 10GB total]
- **Concurrent Operations:** [e.g., single-threaded, 10 concurrent requests]
- **Backend:** [e.g., local filesystem, S3]

## Profiling Data

If you have profiling data, please include it:

```
# Paste profiling output (cProfile, memory_profiler, etc.)
```

**Slow Operations Identified:**

- Function: `nexus.core.something()`
- Time: 4.5 seconds (90% of total time)

## Expected Behavior

**Target Performance:**

What performance would you consider acceptable?

- Duration: [e.g., < 1 second]
- Throughput: [e.g., > 500 MB/s]
- Resource usage: [e.g., < 50% CPU]

## Suggested Optimization

If you have ideas for optimization:

- Caching strategy
- Algorithm improvements
- Batch processing
- Async/concurrent processing
- Database query optimization

## Impact

- **Severity:** [low/medium/high/critical]
- **Frequency:** [rare/occasional/frequent/always]
- **Workaround:** [yes/no] - Is there a workaround available?

## Additional Context

- [ ] I have searched existing issues to ensure this hasn't been reported before
- [ ] I have profiled the code to identify bottlenecks
- [ ] I am willing to help test performance improvements

Add any other context, screenshots, graphs, or benchmarks about the performance issue here.

## Related Issues

Link any related issues or PRs:

- Related to #
- Duplicate of #
