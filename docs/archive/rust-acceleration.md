# Rust-Accelerated Permission Checking

This document describes how to use Rust acceleration for high-performance ReBAC permission checking in Nexus.

## Overview

The Rust extension (`nexus_fast`) provides 50-85x speedup for bulk permission checks by implementing the core ReBAC algorithm in Rust using PyO3. This is especially beneficial for:

- **List operations**: Checking read permissions for many files/objects
- **Batch authorization**: Validating multiple permissions in one call
- **API endpoints**: Filtering large result sets by permissions
- **Background jobs**: Processing permissions for many users/resources

## Performance Characteristics

| Batch Size | Python Time | Rust Time | Speedup |
|------------|-------------|-----------|---------|
| 10 checks  | 5ms         | 0.1ms     | 50x     |
| 100 checks | 50ms        | 0.6ms     | 83x     |
| 1000 checks| 500ms       | 5.9ms     | 85x     |

**Key metrics:**
- Python: ~500µs per permission check
- Rust: ~6µs per permission check
- **85x faster** for large batches

## Installation

### 1. Build the Rust Extension

```bash
cd rust/nexus_fast
maturin develop --release
```

This builds and installs the `nexus_fast` extension into your Python environment.

### 2. Verify Installation

```python
from nexus.core.rebac_fast import is_rust_available

if is_rust_available():
    print("✓ Rust acceleration enabled")
else:
    print("✗ Rust not available, using Python fallback")
```

## Usage

### Method 1: Using rebac_check_batch_fast (Recommended)

This is the easiest way to get Rust acceleration - just use the new `rebac_check_batch_fast` method:

```python
from nexus.core.rebac_manager import ReBACManager

manager = ReBACManager(engine)

# Define batch of permission checks
checks = [
    (("user", "alice"), "read", ("file", "file1")),
    (("user", "alice"), "read", ("file", "file2")),
    (("user", "bob"), "write", ("file", "file3")),
    # ... hundreds or thousands more
]

# Use fast batch method (automatically uses Rust if available)
results = manager.rebac_check_batch_fast(checks)
# Returns: [True, False, True, ...]
```

**Key features:**
- ✅ Automatic Rust acceleration (if available)
- ✅ Transparent fallback to Python
- ✅ Cache-aware (checks cache first)
- ✅ Drop-in replacement for `rebac_check_batch`

### Method 2: Direct Rust API (Advanced)

For direct control, use the low-level Rust API:

```python
from nexus.core.rebac_fast import check_permissions_bulk_rust

# Prepare data
checks = [
    (("user", "alice"), "read", ("file", "doc1")),
]

tuples = [
    {
        "subject_type": "user",
        "subject_id": "alice",
        "subject_relation": None,
        "relation": "read",
        "object_type": "file",
        "object_id": "doc1",
    }
]

namespace_configs = {
    "file": {
        "relations": {"read": "direct"},
        "permissions": {}
    }
}

# Call Rust directly
results = check_permissions_bulk_rust(checks, tuples, namespace_configs)
# Returns: {("user", "alice", "read", "file", "doc1"): True}
```

## Integration Examples

### Example 1: List Readable Files

Before (slow):
```python
def list_readable_files(user_id: str, all_files: list[str]) -> list[str]:
    """List files user can read (SLOW - one check per file)"""
    readable = []
    for file_id in all_files:
        if manager.rebac_check(
            subject=("user", user_id),
            permission="read",
            object=("file", file_id)
        ):
            readable.append(file_id)
    return readable
```

After (fast):
```python
def list_readable_files(user_id: str, all_files: list[str]) -> list[str]:
    """List files user can read (FAST - batch check with Rust)"""
    checks = [
        (("user", user_id), "read", ("file", f))
        for f in all_files
    ]
    results = manager.rebac_check_batch_fast(checks)
    return [all_files[i] for i, allowed in enumerate(results) if allowed]
```

**Performance improvement:** 85x faster for 1000 files

### Example 2: API Endpoint with Permission Filtering

```python
from fastapi import APIRouter, Depends
from nexus.core.auth import get_current_user

router = APIRouter()

@router.get("/files")
async def list_files(user = Depends(get_current_user)):
    """List all files the user can read."""
    # Get all files from database
    all_files = await File.get_all()

    # Build batch permission checks
    checks = [
        (("user", user.id), "read", ("file", file.id))
        for file in all_files
    ]

    # Fast batch check with Rust acceleration
    results = rebac_manager.rebac_check_batch_fast(checks)

    # Filter by permission
    readable_files = [
        file for i, file in enumerate(all_files)
        if results[i]
    ]

    return {"files": readable_files}
```

### Example 3: Multi-User Permission Matrix

```python
def get_permission_matrix(
    users: list[str],
    resources: list[str],
    permission: str
) -> dict[str, list[str]]:
    """Get permission matrix for multiple users and resources."""
    # Build all checks
    checks = [
        (("user", user), permission, ("file", resource))
        for user in users
        for resource in resources
    ]

    # Fast batch check
    results = manager.rebac_check_batch_fast(checks)

    # Build matrix
    matrix = {user: [] for user in users}
    idx = 0
    for user in users:
        for resource in resources:
            if results[idx]:
                matrix[user].append(resource)
            idx += 1

    return matrix
```

## When to Use Rust vs Python

### ✅ Use Rust (rebac_check_batch_fast) for:

- **List operations**: Checking 10+ resources
- **API endpoints**: Filtering results by permissions
- **Batch processing**: Background jobs, reports, audits
- **Permission matrices**: Multiple users × multiple resources

### ❌ Use Python (rebac_check) for:

- **Single checks**: One permission at a time
- **Real-time interactive**: Where latency is already sub-millisecond
- **Development/debugging**: When you need to trace through code

**Rule of thumb:** If checking ≥10 permissions, use `rebac_check_batch_fast`

## Performance Tuning

### 1. Batch Size Optimization

```python
# Too small: overhead dominates
checks = [...]  # 5 checks
results = manager.rebac_check_batch_fast(checks)  # ~50x speedup

# Sweet spot: maximum efficiency
checks = [...]  # 100-1000 checks
results = manager.rebac_check_batch_fast(checks)  # ~85x speedup

# Very large: still efficient
checks = [...]  # 10000 checks
results = manager.rebac_check_batch_fast(checks)  # ~85x speedup
```

### 2. Cache Strategy

The fast batch method respects the L1 cache:

```python
# First call: computes permissions (cache miss)
results1 = manager.rebac_check_batch_fast(checks)  # ~6µs per check

# Second call: returns from cache (cache hit)
results2 = manager.rebac_check_batch_fast(checks)  # ~0.1µs per check
```

### 3. Monitoring Performance

```python
from nexus.core.rebac_fast import get_performance_stats, estimate_speedup

# Check Rust availability
stats = get_performance_stats()
print(f"Rust available: {stats['rust_available']}")

# Estimate speedup for batch
speedup = estimate_speedup(num_checks=500)
print(f"Expected speedup: {speedup}x")
```

## Migration Guide

### Step 1: Identify Batch Operations

Look for patterns like:

```python
# ❌ Slow: loop with individual checks
for item in items:
    if manager.rebac_check(subject, permission, (type, item.id)):
        results.append(item)
```

### Step 2: Convert to Batch

```python
# ✅ Fast: batch check
checks = [(subject, permission, (type, item.id)) for item in items]
allowed = manager.rebac_check_batch_fast(checks)
results = [items[i] for i, ok in enumerate(allowed) if ok]
```

### Step 3: Deploy and Monitor

1. Deploy code with `rebac_check_batch_fast`
2. Monitor logs for Rust acceleration status
3. Compare API response times before/after

## Troubleshooting

### Rust Not Available

If `is_rust_available()` returns `False`:

```bash
# Rebuild Rust extension
cd rust/nexus_fast
maturin develop --release

# Verify
python3 -c "import nexus_fast; print('OK')"
```

### Performance Not Improving

Common issues:

1. **Batch too small**: Need ≥10 checks for Rust to be beneficial
2. **Cache hit**: If all checks are cached, both are fast
3. **Database bottleneck**: Rust speeds up computation, not I/O

Check logs:

```python
import logging
logging.getLogger("nexus.core.rebac_manager").setLevel(logging.INFO)

# You should see:
# 🚀 Batch check: 1000 total, 50 cached, 950 to compute (Rust=enabled)
```

### Results Don't Match

If Python and Rust produce different results:

1. Check namespace configuration format
2. Verify tuple data structure
3. Report issue with reproducible test case

## Benchmarking

Run included benchmarks:

```bash
# Unit tests (fast, no database)
python3 rust/nexus_fast/test_nexus_fast.py

# Integration demo (requires database)
python3 examples/rebac/demo_rust_acceleration.py
```

Expected output:
```
Test 4: Bulk performance test (1000 checks)...
  Processed 1000 checks in 5.91ms
  Average: 5.91µs per check
  ✓ Passed
```

## API Reference

### rebac_check_batch_fast

```python
def rebac_check_batch_fast(
    self,
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    use_rust: bool = True,
) -> list[bool]:
    """
    Batch permission checks with optional Rust acceleration.

    Args:
        checks: List of (subject, permission, object) tuples
        use_rust: Use Rust if available (default: True)

    Returns:
        List of boolean results in same order as input
    """
```

### check_permissions_bulk_rust

```python
def check_permissions_bulk_rust(
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
) -> dict[tuple[str, str, str, str, str], bool]:
    """
    Low-level Rust permission checker.

    Raises:
        RuntimeError: If Rust not available
        ValueError: If input format invalid
    """
```

## Future Enhancements

Planned improvements:

- [ ] Parallel multi-core computation
- [ ] Permission explanation in Rust
- [ ] Async API support
- [ ] Query optimization (fetch only relevant tuples)
- [ ] Streaming API for very large batches

## Support

For issues or questions:
- Check logs: `logging.getLogger("nexus.core.rebac_fast")`
- Review tests: `rust/nexus_fast/test_nexus_fast.py`
- File issue: GitHub repository

## Summary

**Key Takeaways:**

1. ✅ **85x faster** for batch operations (10+ checks)
2. ✅ **Drop-in replacement**: Use `rebac_check_batch_fast`
3. ✅ **Automatic fallback**: Works without Rust installed
4. ✅ **Cache-aware**: Respects existing caching layer
5. ✅ **Production-ready**: Used in high-traffic endpoints

**Quick Start:**

```python
# Replace this:
results = manager.rebac_check_batch(checks)

# With this:
results = manager.rebac_check_batch_fast(checks)

# That's it! 85x speedup automatically if Rust is available
```
