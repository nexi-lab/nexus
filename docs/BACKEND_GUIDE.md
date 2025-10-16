# Backend Usage Guide

Nexus supports pluggable storage backends, allowing you to choose where your files are stored. This guide covers both LocalBackend (default) and GCSBackend (cloud storage).

## Quick Start

### Default: Local Storage

```python
import nexus

# Uses LocalBackend by default
nx = nexus.connect()
nx.write("/workspace/file.txt", b"Hello World")
```

### Cloud Storage: Google Cloud Storage

```python
import nexus
from nexus.backends.gcs import GCSBackend

# Create GCS backend
gcs = GCSBackend(bucket_name="my-bucket")

# Use it with Nexus
nx = nexus.connect(backend=gcs)
nx.write("/workspace/file.txt", b"Stored in the cloud!")
```

## Available Backends

### LocalBackend (Default)

Stores files on the local filesystem using Content-Addressable Storage (CAS).

```python
from nexus.backends.local import LocalBackend

backend = LocalBackend(root_path="./my-data")
nx = nexus.connect(backend=backend)
```

**Features:**
- ✅ Fast local access
- ✅ Automatic deduplication (CAS)
- ✅ Reference counting
- ✅ No network dependency
- ✅ Zero configuration

**Best for:**
- Development environments
- Single-machine deployments
- Low-latency requirements

### GCSBackend

Stores files in Google Cloud Storage bucket using CAS.

```python
from nexus.backends.gcs import GCSBackend

# Option 1: Use Application Default Credentials (gcloud auth)
backend = GCSBackend(bucket_name="my-bucket")

# Option 2: Use explicit credentials
backend = GCSBackend(
    bucket_name="my-bucket",
    project_id="my-project",
    credentials_path="/path/to/service-account-key.json"
)

nx = nexus.connect(backend=backend)
```

**Features:**
- ✅ Cloud storage with global access
- ✅ Automatic deduplication (CAS)
- ✅ Reference counting
- ✅ Scalable and durable
- ✅ Multi-region support

**Best for:**
- Production deployments
- Distributed systems
- Global access requirements
- Cloud-native applications

## GCS Authentication

The GCS backend supports multiple authentication methods:

### Method 1: Application Default Credentials (Recommended for Development)

```bash
# Authenticate with gcloud
gcloud auth application-default login
```

```python
from nexus.backends.gcs import GCSBackend

# Uses ADC automatically
gcs = GCSBackend(bucket_name="my-bucket")
```

### Method 2: Service Account (Recommended for Production)

```bash
# Set environment variable
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
```

```python
from nexus.backends.gcs import GCSBackend

# Uses GOOGLE_APPLICATION_CREDENTIALS env var
gcs = GCSBackend(bucket_name="my-bucket")
```

### Method 3: Explicit Credentials Path

```python
from nexus.backends.gcs import GCSBackend

gcs = GCSBackend(
    bucket_name="my-bucket",
    credentials_path="/path/to/service-account-key.json"
)
```

## Storage Architecture

### Storage Split
- **File content**: Stored in backend (Local filesystem or GCS)
- **Metadata**: Always stored locally in SQLite database

This hybrid approach provides:
- Fast metadata queries (no network calls)
- Scalable content storage
- Flexible backend switching

### GCS Bucket Structure

```
my-bucket/
├── cas/                    # Content storage (by hash)
│   ├── ab/
│   │   └── cd/
│   │       ├── abcd1234...ef56        # Content file
│   │       └── abcd1234...ef56.meta   # Metadata (ref count)
│   └── ...
└── dirs/                   # Directory markers
    └── my-dir/
```

### Content-Addressable Storage (CAS)

Both backends use CAS for automatic deduplication:

```python
# Same content stored only once!
nx.write("/file1.txt", b"same content")
nx.write("/file2.txt", b"same content")  # Deduplicated automatically

# Reference counting
nx.delete("/file1.txt")  # Content still exists (ref_count=1)
nx.delete("/file2.txt")  # Now content is deleted (ref_count=0)
```

**How it works:**
1. Content is hashed with SHA-256
2. Stored using hash as identifier
3. Multiple files can reference the same hash
4. Deletion uses reference counting
5. Content removed only when last reference deleted

## Usage Examples

### Example 1: Environment-Based Backend Selection

```python
import os
import nexus
from nexus.backends.local import LocalBackend
from nexus.backends.gcs import GCSBackend

# Development: Use local storage
if os.environ.get("ENV") == "development":
    backend = LocalBackend(root_path="./dev-data")
else:
    # Production: Use cloud storage
    backend = GCSBackend(bucket_name="prod-bucket")

nx = nexus.connect(backend=backend)
```

### Example 2: Multi-Tenant with GCS

```python
import nexus
from nexus.backends.gcs import GCSBackend

# Shared GCS backend
gcs = GCSBackend(bucket_name="multi-tenant-bucket")

# Tenant A
nx_a = nexus.connect(
    backend=gcs,
    config={"tenant_id": "tenant-a"}
)

# Tenant B
nx_b = nexus.connect(
    backend=gcs,
    config={"tenant_id": "tenant-b"}
)

# Tenants are isolated
nx_a.write("/workspace/tenant-a/data.txt", b"Tenant A data")
nx_b.write("/workspace/tenant-b/data.txt", b"Tenant B data")
```

### Example 3: Hybrid Setup (Cache + Archive)

```python
from nexus.backends.local import LocalBackend
from nexus.backends.gcs import GCSBackend

# Cache: Local storage for hot data
cache = nexus.connect(
    backend=LocalBackend(root_path="./cache"),
    config={"data_dir": "./cache"}
)

# Archive: GCS for cold data
archive = nexus.connect(
    backend=GCSBackend(bucket_name="archive-bucket"),
    config={"data_dir": "./archive-metadata"}
)

# Write to cache
cache.write("/hot/recent.txt", b"Recent data")

# Archive old data to cloud
data = cache.read("/hot/old.txt")
archive.write("/archive/old.txt", data)
cache.delete("/hot/old.txt")
```

### Example 4: Custom Metadata Path

```python
import nexus
from nexus.backends.gcs import GCSBackend

gcs = GCSBackend(bucket_name="my-bucket")
nx = nexus.connect(
    backend=gcs,
    config={
        "data_dir": "/custom/metadata/path",
        "db_path": "/custom/metadata.db"
    }
)
```

## Key Features

### 1. Content Deduplication

Same content is stored only once, regardless of how many files reference it:

```python
# Write same content to multiple files
nx.write("/file1.txt", b"same content")
nx.write("/file2.txt", b"same content")

# Only stored once - automatic deduplication
```

### 2. Reference Counting

Files are only deleted when the last reference is removed:

```python
nx.write("/file1.txt", b"content")
nx.write("/file2.txt", b"content")  # ref_count = 2

nx.delete("/file1.txt")  # ref_count = 1, content still exists
nx.delete("/file2.txt")  # ref_count = 0, content deleted
```

### 3. Metadata Isolation

Metadata is stored locally for fast access:

```python
# Fast metadata queries (no GCS API calls)
exists = nx.exists("/workspace/file.txt")  # Instant
files = nx.list("/workspace")              # Instant

# Content read requires backend access
content = nx.read("/workspace/file.txt")   # Network call for GCS
```

## Best Practices

### 1. Development Environment

Use ADC for simplest authentication:
```bash
gcloud auth application-default login
```

### 2. Production Environment

Use service accounts for better security:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa-key.json"
```

### 3. Reuse Backend Instances

Create backend once and reuse:
```python
gcs = GCSBackend(bucket_name="my-bucket")
nx1 = nexus.connect(backend=gcs)
nx2 = nexus.connect(backend=gcs)  # Reuse same backend
```

### 4. Monitor Costs (GCS)

- Metadata queries are free (local SQLite)
- Content reads/writes incur GCS API charges
- Deduplication reduces storage costs
- Use lifecycle policies for old data

### 5. Bucket Lifecycle (GCS)

Set up lifecycle rules for old content:
```bash
# Delete files older than 90 days
gcloud storage buckets update gs://my-bucket \
  --lifecycle-condition-age=90 \
  --lifecycle-action=Delete
```

## Performance Considerations

### LocalBackend
| Operation | Latency | Notes |
|-----------|---------|-------|
| Metadata queries | ~1ms | In-memory + disk |
| Content read | ~5-10ms | Disk I/O |
| Content write | ~10-20ms | Hash + write |

**Best for:** Low-latency, single-machine deployments

### GCSBackend
| Operation | Latency | Notes |
|-----------|---------|-------|
| Metadata queries | ~1ms | Local SQLite |
| Content read | ~100-500ms | Network + GCS API |
| Content write | ~200-800ms | Hash + GCS API |

**Best for:** Distributed systems, global access

### Cost Optimization (GCS)
1. ✅ Enable deduplication (automatic with CAS)
2. ✅ Use regional buckets for lower latency
3. ✅ Set lifecycle policies for old data
4. ✅ Monitor API usage with Cloud Monitoring
5. ✅ Consider Nearline/Coldline for archives

## Troubleshooting

### GCS: Authentication Error

```
Error: Failed to initialize GCS backend: 403 Forbidden
```

**Solution:**
```bash
# Check authentication
gcloud auth application-default login
gcloud auth list

# Verify credentials
echo $GOOGLE_APPLICATION_CREDENTIALS
```

### GCS: Bucket Not Found

```
Error: Bucket 'my-bucket' does not exist
```

**Solution:**
```bash
# Create the bucket
gcloud storage buckets create gs://my-bucket --location=us-central1

# List existing buckets
gcloud storage buckets list
```

### GCS: Permission Denied

```
Error: 403 The caller does not have permission
```

**Solution:**
```bash
# Grant permissions to service account
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:SA_EMAIL \
  --role=roles/storage.objectAdmin

# Verify permissions
gcloud projects get-iam-policy PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:SA_EMAIL"
```

### Local: Permission Denied

```
Error: Permission denied: '/path/to/nexus-data'
```

**Solution:**
```bash
# Check directory permissions
ls -la /path/to/nexus-data

# Fix permissions
chmod 755 /path/to/nexus-data
```

## Interactive Demo

Run the interactive demo to see all features in action:

```bash
cd nexus
python examples/backend_usage_demo.py
```

The demo covers:
- ✅ Default LocalBackend usage
- ✅ Custom backend configuration
- ✅ GCS cloud storage
- ✅ Content deduplication with CAS
- ✅ Reference counting

## Complete Example

```python
"""Complete example using GCS backend with Nexus."""

import os
import nexus
from nexus.backends.gcs import GCSBackend

def main():
    # Configuration
    bucket_name = os.environ.get("GCS_BUCKET_NAME", "my-nexus-bucket")
    project_id = os.environ.get("GCP_PROJECT_ID")

    # Create GCS backend
    print(f"Connecting to GCS bucket: {bucket_name}")
    gcs = GCSBackend(
        bucket_name=bucket_name,
        project_id=project_id
    )

    # Connect to Nexus
    nx = nexus.connect(
        backend=gcs,
        config={
            "data_dir": "./nexus-metadata",
            "tenant_id": "acme"
        }
    )

    try:
        # Write files
        nx.write("/workspace/acme/report.txt", b"Q4 Report Data")
        nx.write("/workspace/acme/data.json", b'{"revenue": 1000000}')

        # Read back
        report = nx.read("/workspace/acme/report.txt")
        print(f"Report: {report.decode()}")

        # List files
        files = nx.list("/workspace/acme")
        print(f"Files: {files}")

        # Clean up
        for file in files:
            nx.delete(file)

        print("✓ All operations completed successfully")

    finally:
        nx.close()

if __name__ == "__main__":
    main()
```

## Migration Guide

### Switching from Local to GCS

```python
# Before (local only)
nx = nexus.connect()

# After (GCS backend)
from nexus.backends.gcs import GCSBackend
gcs = GCSBackend(bucket_name="my-bucket")
nx = nexus.connect(backend=gcs)
```

**Note:** Metadata is not automatically migrated. You'll need to re-upload files to the new backend.

## See Also

- [GCS Backend Implementation](../src/nexus/backends/gcs.py)
- [Backend Interface](../src/nexus/backends/backend.py)
- [Interactive Demo](../examples/backend_usage_demo.py)
- [Test GCS Backend](../test_gcs_backend.py)
- [Configuration Guide](../examples/config_usage_demo.py)
