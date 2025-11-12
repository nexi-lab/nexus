# Integration Guide: nexus_fast with Nexus

This guide shows how to integrate the high-performance Rust permission checker with the existing Nexus codebase.

## Step 1: Install the Extension

```bash
cd rust/nexus_fast
maturin develop --release
```

Or for production:
```bash
cd rust/nexus_fast
maturin build --release
pip install target/wheels/nexus_fast-*.whl
```

## Step 2: Add Optional Dependency

In the main `pyproject.toml`:

```toml
[project.optional-dependencies]
fast = ["nexus-fast>=0.1.0"]
```

## Step 3: Create Wrapper Module

Create `src/nexus/core/rebac_fast.py`:

```python
"""
Fast ReBAC permission checking with Rust acceleration.
"""
from typing import Dict, List, Tuple, Optional, Any

try:
    import nexus_fast
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False


def convert_to_rust_format(
    checks: List[Tuple[Tuple[str, str], str, Tuple[str, str]]],
    tuples: List[Dict[str, Any]],
    namespace_configs: Dict[str, Any]
) -> Tuple[List, List, Dict]:
    """
    Convert Python data structures to Rust-compatible format.

    This function ensures data is properly formatted for the Rust extension.
    In most cases, no conversion is needed if data is already in the right format.
    """
    # The Rust extension expects exactly the format we use internally,
    # so typically no conversion is needed
    return checks, tuples, namespace_configs


def check_permissions_bulk(
    checks: List[Tuple[Tuple[str, str], str, Tuple[str, str]]],
    tuples: List[Dict[str, Any]],
    namespace_configs: Dict[str, Any],
    force_python: bool = False
) -> Dict[Tuple[str, str, str, str, str], bool]:
    """
    Check multiple permissions in bulk, using Rust if available.

    Args:
        checks: List of (subject, permission, object) tuples
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Namespace configuration for each object type
        force_python: Force use of Python implementation (for testing)

    Returns:
        Dict mapping (subject_type, subject_id, permission, object_type, object_id) -> bool
    """
    if RUST_AVAILABLE and not force_python:
        try:
            checks_rust, tuples_rust, configs_rust = convert_to_rust_format(
                checks, tuples, namespace_configs
            )
            return nexus_fast.compute_permissions_bulk(
                checks_rust, tuples_rust, configs_rust
            )
        except Exception as e:
            # Log warning and fallback to Python
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Rust permission check failed, falling back to Python: {e}")

    # Fallback to Python implementation
    from nexus.core.rebac import check_permissions_python
    return check_permissions_python(checks, tuples, namespace_configs)


def is_rust_available() -> bool:
    """Check if Rust acceleration is available."""
    return RUST_AVAILABLE
```

## Step 4: Update Existing Code

Update `src/nexus/core/rebac.py` to use the fast implementation:

```python
from nexus.core.rebac_fast import check_permissions_bulk, is_rust_available

# For bulk operations, use the fast implementation
def check_user_permissions(user_id: str, operations: List[Tuple[str, str, str]]):
    """
    Check multiple permissions for a user.

    Args:
        user_id: User ID
        operations: List of (permission, object_type, object_id) tuples
    """
    # Convert to bulk check format
    checks = [
        (("user", user_id), perm, (obj_type, obj_id))
        for perm, obj_type, obj_id in operations
    ]

    # Fetch all relevant tuples from database
    tuples = fetch_relevant_tuples(user_id, operations)

    # Get namespace configs
    namespace_configs = get_namespace_configs([op[1] for op in operations])

    # Use fast bulk checker
    results = check_permissions_bulk(checks, tuples, namespace_configs)

    return results
```

## Step 5: Performance Monitoring

Add monitoring to track performance gains:

```python
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

def monitor_permission_check(func):
    """Decorator to monitor permission check performance."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start

        # Log if check took too long
        if elapsed > 0.1:  # 100ms threshold
            logger.warning(f"Slow permission check: {elapsed*1000:.2f}ms")

        return result
    return wrapper

@monitor_permission_check
def check_permissions_monitored(*args, **kwargs):
    return check_permissions_bulk(*args, **kwargs)
```

## Step 6: Testing

Create tests that verify both implementations produce identical results:

```python
import pytest
from nexus.core.rebac_fast import check_permissions_bulk, is_rust_available

@pytest.mark.skipif(not is_rust_available(), reason="Rust extension not available")
def test_rust_python_equivalence():
    """Verify Rust and Python implementations produce identical results."""

    checks = [
        (("user", "alice"), "read", ("file", "doc1")),
        (("user", "bob"), "write", ("file", "doc2")),
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

    namespace_configs = {}

    # Get results from both implementations
    rust_results = check_permissions_bulk(checks, tuples, namespace_configs, force_python=False)
    python_results = check_permissions_bulk(checks, tuples, namespace_configs, force_python=True)

    # They should be identical
    assert rust_results == python_results
```

## Performance Expectations

For typical workloads, you should see:

- **Single permissions**: ~50x speedup (but Python overhead dominates)
- **10-100 permissions**: ~70-80x speedup
- **1000+ permissions**: ~85x speedup

The sweet spot is bulk operations with 100-10,000 permissions.

## When to Use Rust vs Python

**Use Rust (nexus_fast) for:**
- Bulk permission checks (>10 checks)
- API endpoints listing many resources
- Background jobs processing permissions
- Permission audits across many users/objects

**Use Python for:**
- Single permission checks (Python overhead > Rust speedup)
- Interactive debugging
- Code that rarely runs
- Development/testing when Rust build is inconvenient

## Troubleshooting

### Build Errors

If `maturin develop` fails:

1. Check Rust installation: `rustc --version`
2. Update Rust: `rustup update`
3. Check Python version: `python --version` (3.8+ required)
4. Try cleaning build: `cargo clean && maturin develop --release`

### Import Errors

If `import nexus_fast` fails:

1. Verify installation: `pip show nexus-fast`
2. Check Python environment: `which python`
3. Rebuild: `maturin develop --release`

### Incorrect Results

If Rust and Python produce different results:

1. Enable debug logging
2. Add print statements in test_nexus_fast.py
3. Check namespace config format matches expected JSON structure
4. File an issue with reproducible test case

## Example: Real-World Integration

Here's a complete example integrating with a FastAPI endpoint:

```python
from fastapi import APIRouter, Depends
from nexus.core.rebac_fast import check_permissions_bulk, is_rust_available
from nexus.models import User

router = APIRouter()

@router.get("/files")
async def list_files(user: User = Depends(get_current_user)):
    """
    List all files the user can read.
    Uses Rust acceleration for bulk permission checking.
    """
    # Get all files
    all_files = await File.get_all()

    # Build bulk permission checks
    checks = [
        (("user", user.id), "read", ("file", file.id))
        for file in all_files
    ]

    # Fetch relevant ReBAC tuples
    tuples = await fetch_rebac_tuples_for_checks(checks)

    # Get namespace configs
    namespace_configs = get_namespace_config("file")

    # Bulk check permissions (automatically uses Rust if available)
    results = check_permissions_bulk(checks, tuples, {"file": namespace_configs})

    # Filter files by permission
    readable_files = [
        file for file in all_files
        if results.get(("user", user.id, "read", "file", file.id), False)
    ]

    return {
        "files": readable_files,
        "count": len(readable_files),
        "rust_accelerated": is_rust_available()
    }
```

## Migration Path

1. **Phase 1**: Install extension, no code changes (validate it builds)
2. **Phase 2**: Add wrapper module with fallback (safe deployment)
3. **Phase 3**: Update high-traffic endpoints to use bulk checking
4. **Phase 4**: Monitor performance improvements
5. **Phase 5**: Roll out to all permission checks

## Support

For issues or questions:
- Check test_nexus_fast.py for usage examples
- Review README.md for API documentation
- File issues in the Nexus GitHub repository
