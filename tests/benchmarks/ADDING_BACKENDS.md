# Adding Custom Backends to Benchmarks

This guide explains how to add new storage backends or metadata stores to the benchmark suite.

## Architecture Overview

Nexus has a **two-layer architecture**:

```
┌─────────────────────────────────────┐
│         NexusFS                     │
│  (Unified file system interface)   │
└─────────────┬───────────────────────┘
              │
      ┌───────┴────────┐
      │                │
      ▼                ▼
┌──────────┐    ┌──────────────┐
│ Storage  │    │   Metadata   │
│ Backend  │    │    Store     │
└──────────┘    └──────────────┘
  (Content)       (Metadata)
```

### 1. Storage Backend (Content Layer)
Where actual file **CONTENT** is stored:
- `LocalBackend`: Local disk
- `GCSBackend`: Google Cloud Storage
- `S3Backend`: Amazon S3 (future)

### 2. Metadata Store (Metadata Layer)
Where file **METADATA** is stored:
- `SQLite`: Embedded database (default)
- `PostgreSQL`: Scalable database for production

## Current Benchmark Combinations

The benchmark suite tests these combinations:

| Storage Backend | Metadata Store | Combination Name | When Available |
|----------------|----------------|------------------|----------------|
| LocalBackend | SQLite | `local-sqlite` | Always (default) |
| LocalBackend | PostgreSQL | `local-postgres` | If `NEXUS_DATABASE_URL` set |
| GCSBackend | SQLite | `gcs-sqlite` | If `GCS_BUCKET` set |
| GCSBackend | PostgreSQL | `gcs-postgres` | If both env vars set |
| *(baseline)* | *(none)* | `local_fs` | Always (raw filesystem) |

## How to Add a New Storage Backend

### Example: Adding S3Backend

**Step 1**: Edit `tests/benchmarks/conftest.py`

Add S3 to the combinations function:

```python
def get_backend_combinations() -> list[str]:
    """Get available backend/metadata combinations for testing."""
    combinations = ["local-sqlite", "local_fs"]

    # ... existing code ...

    # Add S3 backend combinations
    if os.getenv("AWS_BUCKET") and os.getenv("AWS_ACCESS_KEY_ID"):
        combinations.append("s3-sqlite")
        if os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL"):
            combinations.append("s3-postgres")

    return combinations
```

**Step 2**: Add S3 backend creation in `nexus_backend` fixture:

```python
@pytest.fixture
def nexus_backend(backend_type: str, temp_dir: Path) -> Generator[NexusFS | Path, None, None]:
    # ... existing code ...

    # Create storage backend
    if storage_backend == "local":
        backend = LocalBackend(temp_dir / "nexus-data")
    elif storage_backend == "gcs":
        backend = GCSBackend(...)
    elif storage_backend == "s3":
        from nexus.backends.s3 import S3Backend

        bucket = os.getenv("AWS_BUCKET")
        if not bucket:
            pytest.skip("AWS_BUCKET not configured")

        backend = S3Backend(
            bucket_name=bucket,
            region=os.getenv("AWS_REGION", "us-west-2"),
            base_path=f"benchmark-{temp_dir.name}",
        )
    else:
        pytest.skip(f"Unknown storage backend: {storage_backend}")

    # ... rest of fixture ...
```

**Step 3**: Set environment variables and run:

```bash
export AWS_BUCKET=my-test-bucket
export AWS_ACCESS_KEY_ID=xxx
export AWS_SECRET_ACCESS_KEY=xxx
export AWS_REGION=us-west-2

# Run benchmarks (will now include s3-sqlite)
bash scripts/run_benchmarks.sh
```

## How to Add a New Metadata Store

### Example: Adding Redis for metadata caching

If you want to test a Redis-backed metadata store:

**Step 1**: Add combination:

```python
def get_backend_combinations() -> list[str]:
    combinations = ["local-sqlite", "local_fs"]

    # Add Redis metadata if available
    if os.getenv("REDIS_URL"):
        combinations.append("local-redis")

    return combinations
```

**Step 2**: Handle in fixture:

```python
# Create metadata store path
if metadata_store == "sqlite":
    db_path = temp_dir / "metadata.db"
elif metadata_store == "postgres":
    db_path = os.getenv("NEXUS_DATABASE_URL")
elif metadata_store == "redis":
    db_path = os.getenv("REDIS_URL")  # Custom handling in NexusFS
else:
    pytest.skip(f"Unknown metadata store: {metadata_store}")
```

## Environment Variables Reference

### Always Available
- None needed for `local-sqlite` (default)
- None needed for `local_fs` (raw filesystem baseline)

### PostgreSQL Metadata
```bash
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
# OR
export POSTGRES_URL="postgresql://user:pass@localhost/nexus"
```

### GCS Storage Backend
```bash
export GCS_BUCKET="my-benchmark-bucket"
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
```

### S3 Storage Backend (example)
```bash
export AWS_BUCKET="my-benchmark-bucket"
export AWS_ACCESS_KEY_ID="xxx"
export AWS_SECRET_ACCESS_KEY="xxx"
export AWS_REGION="us-west-2"
```

## Running Benchmarks with Specific Backends

### Test only local-sqlite (default)
```bash
pytest tests/benchmarks/ --benchmark-only -k "local-sqlite"
```

### Test only PostgreSQL metadata combinations
```bash
export NEXUS_DATABASE_URL="postgresql://localhost/nexus"
pytest tests/benchmarks/ --benchmark-only -k "postgres"
```

### Test only GCS backend combinations
```bash
export GCS_BUCKET="test-bucket"
export GOOGLE_APPLICATION_CREDENTIALS="creds.json"
pytest tests/benchmarks/ --benchmark-only -k "gcs"
```

### Compare specific backends
```bash
# Compare local-sqlite vs local-postgres
export NEXUS_DATABASE_URL="postgresql://localhost/nexus"
pytest tests/benchmarks/test_throughput.py --benchmark-only \
  --benchmark-compare \
  --benchmark-group-by=param:backend_type
```

## Expected Results

### Local vs GCS Backend
- **Local**: Fastest for content operations (local disk I/O)
- **GCS**: Network latency overhead, but scalable and durable

### SQLite vs PostgreSQL Metadata
- **SQLite**: Faster for single-writer workloads, lower latency
- **PostgreSQL**: Better for concurrent writes, scalable for multi-agent systems

## Example: Full Matrix Test

To test ALL combinations:

```bash
# Set up all backends
export NEXUS_DATABASE_URL="postgresql://localhost/nexus"
export GCS_BUCKET="my-bucket"
export GOOGLE_APPLICATION_CREDENTIALS="creds.json"

# Run full suite
bash scripts/run_benchmarks.sh

# Results will include:
# - local-sqlite (LocalBackend + SQLite)
# - local-postgres (LocalBackend + PostgreSQL)
# - gcs-sqlite (GCSBackend + SQLite)
# - gcs-postgres (GCSBackend + PostgreSQL)
# - local_fs (raw filesystem baseline)
```

## Benchmark Results Interpretation

When comparing backends, focus on:

1. **Content operations** (read/write throughput)
   - Primarily affected by **storage backend** (Local vs GCS)
   - Network latency matters for cloud backends

2. **Metadata operations** (exists, list, versioning)
   - Primarily affected by **metadata store** (SQLite vs PostgreSQL)
   - PostgreSQL better for concurrent access

3. **Deduplication efficiency**
   - Same across all backends (CAS is backend-agnostic)
   - Storage savings apply regardless of backend choice

## Troubleshooting

### Backend not appearing in tests
```bash
# Check what backends are available
pytest tests/benchmarks/conftest.py::test_available_backends -v

# Or run with verbose fixture info
pytest tests/benchmarks/ --benchmark-only --setup-show
```

### Skipped tests
If you see "SKIPPED: Backend not configured", check:
- Environment variables are set correctly
- Credentials are valid
- Services are accessible (network connectivity for cloud backends)

### Performance debugging
```bash
# Run single backend with verbose output
pytest tests/benchmarks/test_throughput.py::TestWriteThroughput::test_write_1kb \
  -k "local-sqlite" \
  --benchmark-only \
  --benchmark-verbose
```

## Contributing New Backends

When adding a new backend to Nexus core:

1. Implement the `Backend` protocol in `src/nexus/backends/`
2. Add benchmark support in `tests/benchmarks/conftest.py`
3. Document environment variables in this file
4. Add example results to `tests/benchmarks/RESULTS.md`
5. Update `.github/workflows/` CI to optionally test new backend

Questions? See the benchmark README or ask in GitHub issues!
