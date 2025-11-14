# nexus_fast

High-performance Rust implementations for Nexus core operations.

## Overview

`nexus_fast` is a Python extension module written in Rust using PyO3. It provides blazing-fast implementations of:

1. **ReBAC Permission Computation** - 10-100x faster permission checking
2. **Content Search (grep)** - 30-100x faster regex-based file searching

## Performance

### ReBAC Permission Checking
- **~6µs per permission check** (tested on Apple M-series)
- **170,000+ checks per second** on a single core

### Content Search (grep)
- **~186,000 files per second** for pattern matching
- **30-100x faster** than Python regex
- **5ms to search 1000 files** with 30,000 lines

All operations use GIL-free computation for true parallel execution with zero-copy data structures.

## Features

### ReBAC Acceleration
- ✅ Direct relation checks
- ✅ Union relations (OR semantics)
- ✅ Tuple-to-userset (parent/child relationships)
- ✅ Memoization cache for repeated checks
- ✅ Cycle detection to prevent infinite loops
- ✅ Bulk permission computation
- ✅ Namespace configuration support

### Grep Acceleration
- ✅ Fast regex-based content search
- ✅ Case-sensitive and case-insensitive matching
- ✅ Binary file detection and skipping
- ✅ UTF-8 text processing
- ✅ Batch file processing
- ✅ Max results limiting

## Installation

### Option 1: Use Without Rust (Automatic Fallback)

When you install Nexus from PyPI, the Rust extension is **optional**. If not available, Nexus automatically falls back to Python implementations:

```bash
pip install nexus-ai-fs
# Grep will use Python regex (still fast, just 30x slower)
```

### Option 2: Build Rust Extension for Maximum Performance

For 30-100x speedup on grep operations:

#### Prerequisites

- Rust toolchain (install via [rustup](https://rustup.rs/))
- Python 3.8+
- maturin (`pip install maturin`)

#### Build Steps

```bash
# Navigate to the Rust extension directory
cd rust/nexus_fast

# Build and install in development mode
maturin develop --release

# Or build a wheel for distribution
maturin build --release
pip install target/wheels/*.whl
```

#### Verify Installation

```python
from nexus.core import grep_fast

print(grep_fast.is_available())  # Should print True
```

**Note:** The Rust extension must be rebuilt after:
- Pulling new code changes
- Switching branches with Rust changes
- Updating Rust toolchain

## Usage

```python
import nexus_fast

# Define permission checks: [(subject, permission, object), ...]
checks = [
    (("user", "alice"), "read", ("file", "doc1")),
    (("user", "bob"), "write", ("file", "doc2")),
]

# Define ReBAC tuples (relationships)
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

# Define namespace configurations (optional)
namespace_configs = {
    "file": {
        "relations": {
            "reader": "direct",
            "writer": "direct",
            "owner": {"union": ["reader", "writer"]}
        },
        "permissions": {
            "view": ["reader", "owner"],
            "edit": ["writer", "owner"]
        }
    }
}

# Compute permissions
results = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)

# Results is a dict mapping (subject_type, subject_id, permission, object_type, object_id) -> bool
print(results[("user", "alice", "read", "file", "doc1")])  # True/False
```

## API Reference

### `compute_permissions_bulk(checks, tuples, namespace_configs) -> dict`

Compute multiple permission checks in bulk.

### `grep_bulk(pattern, file_contents, ignore_case=False, max_results=1000) -> list`

Fast regex-based content search across multiple files.

**Parameters:**
- `pattern`: Regex pattern string to search for
- `file_contents`: Dict mapping file paths to file content bytes `{"/path/file.txt": b"content"}`
- `ignore_case`: Boolean, whether to ignore case (default: False)
- `max_results`: Maximum number of matches to return (default: 1000)

**Returns:**
- List of match dictionaries:
  ```python
  [
      {
          "file": "/path/file.txt",
          "line": 42,                 # 1-indexed line number
          "content": "full line text",
          "match": "matched text"
      }
  ]
  ```

**Example:**
```python
import nexus_fast

files = {
    "/test.py": b"def hello():\n    print('Hello')\n",
    "/main.py": b"from test import hello\n"
}

# Case-sensitive search
results = nexus_fast.grep_bulk(r"def \w+", files)

# Case-insensitive search
results = nexus_fast.grep_bulk("hello", files, ignore_case=True)
```

---

### ReBAC API

### `compute_permissions_bulk(checks, tuples, namespace_configs) -> dict`

Compute multiple permission checks in bulk.

**Parameters:**
- `checks`: List of tuples `[(subject, permission, object), ...]` where:
  - `subject`: `(subject_type: str, subject_id: str)`
  - `permission`: `str`
  - `object`: `(object_type: str, object_id: str)`

- `tuples`: List of ReBAC relationship dictionaries:
  ```python
  {
      "subject_type": str,
      "subject_id": str,
      "subject_relation": Optional[str],
      "relation": str,
      "object_type": str,
      "object_id": str,
  }
  ```

- `namespace_configs`: Dict mapping object types to their namespace configuration:
  ```python
  {
      "object_type": {
          "relations": {
              "relation_name": "direct" | {"union": [rel1, rel2]} |
                              {"tupleToUserset": {"tupleset": str, "computedUserset": str}}
          },
          "permissions": {
              "permission_name": [userset1, userset2]
          }
      }
  }
  ```

**Returns:**
- Dict mapping `(subject_type, subject_id, permission, object_type, object_id)` -> `bool`

## Testing

Run the included test suite:

```bash
python test_nexus_fast.py
```

Expected output:
```
============================================================
Testing nexus_fast Rust extension
============================================================
Test 1: Basic direct permission...
  ✓ Passed

Test 2: Permission with namespace (union relation)...
  ✓ Passed

Test 3: TupleToUserset (parent folder permissions)...
  ✓ Passed

Test 4: Bulk performance test (1000 checks)...
  Processed 1000 checks in 5.91ms
  Average: 5.91µs per check
  ✓ Passed

Test 5: Permission denial (negative case)...
  ✓ Passed

============================================================
All tests passed! ✓
============================================================
```

## Architecture

### Core Components

1. **Entity**: Represents subjects and objects with type and ID
2. **ReBACTuple**: Relationship between entities
3. **NamespaceConfig**: Permission expansion rules
4. **MemoCache**: Caching layer for repeated permission checks

### Algorithm

The permission checker implements a recursive graph traversal algorithm with:

1. **Memoization**: Cache results to avoid redundant computation
2. **Cycle Detection**: Track visited nodes to prevent infinite loops
3. **Depth Limiting**: Max recursion depth of 50 to prevent stack overflow
4. **GIL Release**: Uses `py.allow_threads()` for parallel execution

### Performance Optimizations

- **AHashMap**: Fast hash table implementation (vs. std HashMap)
- **Zero-copy**: Minimize data copying between Python and Rust
- **Batch Processing**: Compute multiple checks in one call
- **Early Exit**: Stop checking on first match for OR semantics
- **LTO**: Link-time optimization enabled in release builds

## Integration with Nexus

To integrate with the Nexus Python codebase:

```python
from nexus.core.rebac import check_permissions_python  # existing
import nexus_fast

def check_permissions_optimized(checks, tuples, namespace_configs):
    """
    Fast permission checking with fallback to Python implementation.
    """
    try:
        return nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    except Exception as e:
        # Fallback to Python implementation
        return check_permissions_python(checks, tuples, namespace_configs)
```

## Benchmarks

| Check Count | Python (ms) | Rust (ms) | Speedup |
|------------|-------------|-----------|---------|
| 100        | 50          | 0.6       | 83x     |
| 1,000      | 500         | 5.9       | 85x     |
| 10,000     | 5,000       | 59        | 85x     |

*Benchmarked on Apple M1 Pro*

## Development

### Build Requirements

- Rust 1.70+ (with cargo)
- Python 3.8+
- PyO3 0.22
- maturin 1.0+

### Project Structure

```
rust/nexus_fast/
├── Cargo.toml              # Rust dependencies and configuration
├── pyproject.toml          # Python packaging configuration
├── src/
│   └── lib.rs             # Main implementation
├── test_nexus_fast.py     # Test suite
└── README.md              # This file
```

### Building

```bash
# Development build (debug)
maturin develop

# Release build (optimized)
maturin develop --release

# Create wheel for distribution
maturin build --release
```

## License

Same as parent Nexus project.

## Contributing

When modifying the Rust code:

1. Run tests: `python test_nexus_fast.py`
2. Check formatting: `cargo fmt`
3. Run linter: `cargo clippy`
4. Rebuild: `maturin develop --release`

## Future Enhancements

- [ ] Support for computed usersets
- [ ] Wildcard relation matching
- [ ] Permission explanation (why was access granted/denied?)
- [ ] Parallel bulk checking across multiple cores
- [ ] Integration with Nexus async runtime
