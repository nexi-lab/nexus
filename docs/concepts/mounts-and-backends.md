# Mounts & Backends

## What are Mounts and Backends?

**Backends** are storage systems (local filesystem, S3, GCS, databases) that store your actual data. **Mounts** route virtual filesystem paths to specific backends, allowing you to use multiple storage systems through a unified API.

### Traditional Approach vs Mounts

| Traditional | With Mounts |
|-------------|-------------|
| ❌ Hardcode storage location | ✅ Configure at runtime |
| ❌ Rewrite code to switch storage | ✅ Change mount, keep code |
| ❌ One storage system per app | ✅ Multiple backends simultaneously |
| ❌ Manual routing logic | ✅ Automatic path-based routing |

**Key Innovation:** One filesystem API, multiple storage backends.

---

## Core Concepts

### Backends (Physical Storage)

A **backend** is where data is actually stored:

```python
from nexus.backends import LocalBackend, GCSBackend

# Local filesystem backend
local = LocalBackend(root_dir="/var/nexus/data")

# Google Cloud Storage backend
gcs = GCSBackend(bucket="my-bucket", project="my-project")
```

**Available backends:**
- `LocalBackend` - Local filesystem
- `GCSBackend` - Google Cloud Storage
- Custom backends (S3, Azure, PostgreSQL, Redis, MongoDB)

---

### Mounts (Routing Rules)

A **mount** maps a virtual path to a backend:

```python
# Mount local backend at /workspace
nx.mount("/workspace", local_backend)

# Mount GCS backend at /cloud
nx.mount("/cloud", gcs_backend)

# Now use both through same API:
nx.write("/workspace/local.txt", b"Local file")
nx.write("/cloud/remote.txt", b"Cloud file")
```

**Architecture:**

```mermaid
graph LR
    A[Agent writes to<br/>/workspace/file.txt] --> R[PathRouter]
    B[Agent writes to<br/>/cloud/data.json] --> R

    R -->|matches /workspace| L[LocalBackend<br/>/var/nexus/data]
    R -->|matches /cloud| G[GCSBackend<br/>gs://my-bucket]

    L -->|writes| D1[/var/nexus/data/cas/ab/cd/...]
    G -->|uploads| D2[gs://my-bucket/cas/ab/cd/...]
```

---

## Path Routing

### How Routing Works

The **PathRouter** uses **longest-prefix matching** (like IP routing):

```python
# Mount configuration
nx.mount("/workspace", local_backend, priority=0)
nx.mount("/workspace/shared", gcs_backend, priority=10)
nx.mount("/cloud", gcs_backend, priority=0)

# Routing examples
nx.write("/workspace/file.txt", ...)        # → local_backend
nx.write("/workspace/shared/doc.txt", ...)  # → gcs_backend (longer prefix)
nx.write("/cloud/data.json", ...)           # → gcs_backend
```

**Routing algorithm:**

1. **Get all mounts** matching path prefix
2. **Sort by priority** (DESC), then **prefix length** (DESC)
3. **Return first match** (longest, highest priority)

---

### Mount Priority

When multiple mounts match, priority determines winner:

```python
# Both match /workspace/file.txt
nx.mount("/workspace", backend_a, priority=0)
nx.mount("/workspace", backend_b, priority=10)  # Higher priority wins

nx.write("/workspace/file.txt", ...)  # → backend_b (priority=10)
```

**Use case:** Override specific paths with higher-priority mounts.

---

## Backend Types

### LocalBackend

Stores files on local filesystem with Content-Addressable Storage (CAS):

```python
from nexus.backends import LocalBackend

backend = LocalBackend(
    root_dir="/var/nexus/data",
    content_cache_size_mb=256  # Optional LRU cache
)

nx.mount("/workspace", backend)
```

**Directory structure:**
```
/var/nexus/data/
├── cas/                    # Content-addressable storage
│   ├── ab/
│   │   └── cd/
│   │       ├── abcd1234...  # Content file
│   │       └── abcd1234....meta  # Metadata (ref_count, size)
└── metadata.db             # SQLite metadata store
```

**Features:**
- SHA-256 content hashing
- Automatic deduplication
- Reference counting
- Thread-safe file locking
- Cross-platform (Windows, macOS, Linux)

---

### GCSBackend

Stores files in Google Cloud Storage:

```python
from nexus.backends import GCSBackend

backend = GCSBackend(
    bucket="my-bucket",
    project="my-project",          # Optional
    credentials_path="/path/to/service-account.json"  # Optional
)

nx.mount("/cloud", backend)
```

**Authentication methods** (priority order):
1. `credentials_path` parameter
2. `GOOGLE_APPLICATION_CREDENTIALS` env var
3. Application Default Credentials (gcloud auth)
4. GCE/Cloud Run service account

**Storage structure:**
```
gs://my-bucket/
├── cas/ab/cd/abcd1234...       # Content blob
└── cas/ab/cd/abcd1234....meta  # Metadata blob
```

**Features:**
- Unlimited scalability
- Global distribution
- Automatic redundancy
- Same CAS as LocalBackend

---

### Custom Backends

Implement the `Backend` interface:

```python
from nexus.backends import Backend
from nexus.core.permissions import OperationContext

class S3Backend(Backend):
    @property
    def name(self) -> str:
        return "s3"

    def write_content(self, content: bytes, context: OperationContext) -> str:
        """Write content, return SHA-256 hash."""
        content_hash = hashlib.sha256(content).hexdigest()
        # Upload to S3...
        return content_hash

    def read_content(self, content_hash: str, context: OperationContext) -> bytes:
        """Read content by hash."""
        # Download from S3...
        return content

    def delete_content(self, content_hash: str, context: OperationContext) -> None:
        """Delete content (ref counting)."""
        # Delete from S3...
        pass

    def content_exists(self, content_hash: str, context: OperationContext) -> bool:
        """Check if content exists."""
        # Check S3...
        return True
```

---

## Mount Management

### Creating Mounts

```python
# Python API
nx.mount(
    path="/workspace",
    backend=local_backend,
    priority=0,          # Optional, default=0
    enabled=True,        # Optional, default=True
    metadata={}          # Optional metadata
)
```

```bash
# CLI (future)
nexus mounts add /workspace local --root /var/nexus/data
nexus mounts add /cloud gcs --bucket my-bucket
```

---

### Listing Mounts

```python
# Get all mounts
mounts = nx.list_mounts()

for mount in mounts:
    print(f"{mount['path']} → {mount['backend_name']} (priority={mount['priority']})")

# Output:
# /workspace/shared → gcs (priority=10)
# /workspace → local (priority=0)
# /cloud → gcs (priority=0)
```

---

### Removing Mounts

```python
# Unmount by path
nx.unmount("/workspace/shared")

# Unmount all
for mount in nx.list_mounts():
    nx.unmount(mount['path'])
```

---

## Built-in Namespaces

Nexus provides **5 built-in namespaces** for common use cases:

| Namespace | Path Format | Read-Only | Purpose |
|-----------|-------------|-----------|---------|
| **workspace** | `/workspace/{path}` | No | User workspaces with ReBAC |
| **shared** | `/shared/{tenant}/{path}` | No | Tenant shared data |
| **external** | `/external/{path}` | No | Pass-through backends |
| **system** | `/system/{path}` | Yes | System metadata (admin-only) |
| **archives** | `/archives/{tenant}/{path}` | Yes | Read-only cold storage |

### workspace Namespace

User-specific workspaces with fine-grained permissions:

```python
# Alice's workspace
nx.write("/workspace/alice/notes.txt", b"My notes")

# Bob's workspace
nx.write("/workspace/bob/project.md", b"Project docs")

# ReBAC controls who can access what
nx.rebac.grant("user", "alice", "owner", "file", "/workspace/alice/")
```

---

### shared Namespace

Tenant-wide shared storage:

```python
# Shared team files
nx.write("/shared/acme/team-docs/policy.pdf", pdf_data)

# All acme tenant users can access (based on ReBAC)
nx.rebac.grant("group", "acme-team", "viewer", "file", "/shared/acme/")
```

---

### external Namespace

Direct backend pass-through (no ReBAC, minimal overhead):

```python
# Mount external data source
nx.mount("/external/raw-data", s3_backend)

# Fast read/write without permission checks
data = nx.read("/external/raw-data/sensor-logs.csv")
```

---

### system Namespace

System metadata (read-only, admin access only):

```python
# System config (read-only)
config = nx.read("/system/config/settings.json")

# Write forbidden (even for admins, use special API)
nx.write("/system/config/settings.json", ...)  # ❌ Raises error
```

---

### archives Namespace

Historical/read-only data:

```python
# Mount archive backend
nx.mount("/archives/acme", glacier_backend)

# Read archived data
old_data = nx.read("/archives/acme/2023/reports/q1.pdf")

# Write forbidden
nx.write("/archives/acme/new.pdf", ...)  # ❌ Read-only namespace
```

---

## Multi-Backend Scenarios

### Scenario 1: Local + Cloud Hybrid

```python
# Local for hot data
local = LocalBackend("/var/nexus/hot")
nx.mount("/workspace", local)

# Cloud for cold storage
gcs = GCSBackend("my-archive-bucket")
nx.mount("/archives", gcs)

# Use both
nx.write("/workspace/active.txt", b"Working on this")
old_data = nx.read("/archives/2023/report.pdf")
```

---

### Scenario 2: Multi-Region

```python
# US region
us_gcs = GCSBackend("us-bucket", region="us-east1")
nx.mount("/us", us_gcs)

# EU region
eu_gcs = GCSBackend("eu-bucket", region="europe-west1")
nx.mount("/eu", eu_gcs)

# Route by geography
nx.write("/us/user-data.json", us_user_data)
nx.write("/eu/user-data.json", eu_user_data)
```

---

### Scenario 3: Development → Production

```python
# Development: local backend
if env == "dev":
    nx.mount("/workspace", LocalBackend("/tmp/nexus-dev"))

# Production: cloud backend
elif env == "prod":
    nx.mount("/workspace", GCSBackend("prod-bucket"))

# Code stays the same!
nx.write("/workspace/file.txt", data)
```

---

## Content-Addressable Storage (CAS)

### How CAS Works

All backends use **SHA-256 content hashing** for storage:

```python
# Write file
content = b"Hello World"
hash = hashlib.sha256(content).hexdigest()
# → "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"

# Storage path
# cas/a5/91/a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e
```

**Benefits:**
- ✅ **Automatic deduplication** - Same content stored once
- ✅ **Immutable history** - Content never changes (hash = identity)
- ✅ **Efficient storage** - No duplicate data
- ✅ **Integrity verification** - Hash mismatch = corruption

---

### Reference Counting

Each content blob has a **reference count**:

```python
# First write
nx.write("/file1.txt", b"content")  # ref_count=1

# Same content, different path
nx.write("/file2.txt", b"content")  # ref_count=2 (deduped!)

# Delete first file
nx.delete("/file1.txt")  # ref_count=1 (content remains)

# Delete second file
nx.delete("/file2.txt")  # ref_count=0 (content deleted)
```

**Metadata file:**
```json
{
  "ref_count": 2,
  "size": 7
}
```

---

## Virtual Paths vs Physical Storage

### Virtual Filesystem

Users interact with **virtual paths**:

```python
nx.write("/workspace/alice/document.pdf", pdf_data)
nx.write("/cloud/backup.zip", zip_data)
```

### Physical Storage

Data is stored by **content hash**:

```
# LocalBackend physical layout
/var/nexus/data/cas/ab/cd/abcd1234...  # document.pdf content
/var/nexus/data/cas/ef/gh/efgh5678...  # backup.zip content

# GCSBackend physical layout
gs://my-bucket/cas/ab/cd/abcd1234...
gs://my-bucket/cas/ef/gh/efgh5678...
```

### Mapping

**Metadata store** maps virtual → physical:

```python
{
  "virtual_path": "/workspace/alice/document.pdf",
  "content_hash": "abcd1234...",  # Physical CAS location
  "backend_id": "local",
  "size": 102400,
  "version": 1
}
```

---

## Configuration Examples

### Basic Setup (Local Only)

```python
from nexus import NexusFS, LocalBackend
from nexus.backends import LocalBackend

# Create backend
backend = LocalBackend("/var/nexus/data")

# Create filesystem
nx = NexusFS(backend=backend, is_admin=True)

# Use it
nx.write("/file.txt", b"content")
```

---

### Multi-Backend Setup

```python
from nexus import NexusFS, LocalBackend
from nexus.backends import LocalBackend, GCSBackend

# Create filesystem (no default backend)
backend = LocalBackend(root_path="/tmp/nexus-data")
nx = NexusFS(backend=backend, is_admin=True)

# Mount local for workspace
local = LocalBackend("/var/nexus/local")
nx.mount("/workspace", local)

# Mount GCS for cloud storage
gcs = GCSBackend("my-bucket", project="my-project")
nx.mount("/cloud", gcs)

# Mount GCS for archives (same backend, different mount)
nx.mount("/archives", gcs, priority=5)

# Use all mounts
nx.write("/workspace/local.txt", b"local")
nx.write("/cloud/remote.json", b'{"cloud": true}')
nx.write("/archives/old-data.csv", b"archived")
```

---

### Priority Override

```python
# Default: all workspace files go to local
nx.mount("/workspace", local_backend, priority=0)

# Override: /workspace/shared goes to cloud
nx.mount("/workspace/shared", gcs_backend, priority=10)

# Routing
nx.write("/workspace/file.txt", ...)         # → local (priority=0)
nx.write("/workspace/shared/doc.txt", ...)  # → gcs (priority=10, longer prefix)
```

---

## CLI Commands

### List Mounts

```bash
nexus mounts list

# Output:
# PATH                    BACKEND    PRIORITY  ENABLED
# /workspace/shared       gcs        10        yes
# /workspace              local      0         yes
# /cloud                  gcs        0         yes
```

---

### Add Mount

```bash
# Add local mount
nexus mounts add /workspace local --root /var/nexus/data

# Add GCS mount
nexus mounts add /cloud gcs --bucket my-bucket --project my-project

# Add with priority
nexus mounts add /workspace/shared gcs --bucket shared-bucket --priority 10
```

---

### Remove Mount

```bash
# Remove by path
nexus mounts remove /workspace/shared

# Confirm removal
nexus mounts list
```

---

### FUSE Mount (Linux/macOS)

```bash
# Mount Nexus as local filesystem
nexus fuse mount /mnt/nexus

# Now use normal tools
ls /mnt/nexus/workspace/
cat /mnt/nexus/workspace/file.txt
cp myfile.txt /mnt/nexus/workspace/

# Unmount
fusermount -u /mnt/nexus  # Linux
umount /mnt/nexus         # macOS
```

---

## Best Practices

### 1. Use Namespaces

```python
# ✅ Good: Organized by namespace
nx.mount("/workspace", local_backend)
nx.mount("/archives", gcs_backend)

# ❌ Bad: Flat structure
nx.mount("/", mixed_backend)  # Everything in one backend
```

---

### 2. Separate Hot and Cold Data

```python
# ✅ Good: Hot data local, cold data cloud
nx.mount("/workspace", LocalBackend("/var/nexus/hot"))
nx.mount("/archives", GCSBackend("cold-storage"))

# ❌ Bad: All data in expensive cloud storage
nx.mount("/", GCSBackend("expensive-bucket"))
```

---

### 3. Use Priority for Overrides

```python
# ✅ Good: Override specific paths
nx.mount("/workspace", local, priority=0)
nx.mount("/workspace/shared", gcs, priority=10)  # Override

# ❌ Bad: Create new top-level path
nx.mount("/workspace-shared", gcs)  # Inconsistent naming
```

---

### 4. Test Before Production

```python
# ✅ Good: Environment-based config
backend = (
    LocalBackend("/tmp/nexus-dev") if env == "dev"
    else GCSBackend("prod-bucket")
)
nx.mount("/workspace", backend)

# ❌ Bad: Hardcode production
nx.mount("/workspace", GCSBackend("prod-bucket"))  # Always prod
```

---

### 5. Monitor Backend Health

```python
# Check backend connectivity
try:
    backend.content_exists("test-hash")
except Exception as e:
    logger.error(f"Backend unhealthy: {e}")
    # Fallback or alert
```

---

## Performance Considerations

### LocalBackend

- **Read**: ~5ms (uncached), ~0.1ms (cached)
- **Write**: ~10ms (includes hash computation)
- **Cache**: 256MB LRU cache by default

### GCSBackend

- **Read**: ~50-200ms (network latency)
- **Write**: ~100-500ms (upload time)
- **Batch**: Use `batch_read_content()` for multiple files

### Optimization Tips

1. **Cache hot data** - Enable content cache for frequently accessed files
2. **Batch operations** - Read multiple files in one call
3. **Use local for hot, cloud for cold** - Hybrid approach
4. **Monitor latency** - Alert on slow backend responses

---

## Troubleshooting

### Mount Not Found

**Problem:** `nx.write("/path/file.txt", ...)` raises "No mount found"

**Fix:** Check mounts:
```python
mounts = nx.list_mounts()
print(mounts)  # Is /path mounted?

# Add mount
nx.mount("/path", backend)
```

---

### Wrong Backend

**Problem:** File goes to wrong backend

**Fix:** Check mount priority:
```python
# List mounts (sorted by priority)
for mount in nx.list_mounts():
    print(f"{mount['path']} (priority={mount['priority']})")

# Higher priority wins
nx.mount("/workspace", backend_a, priority=0)
nx.mount("/workspace", backend_b, priority=10)  # This wins
```

---

### Backend Connection Failed

**Problem:** GCS authentication fails

**Fix:** Check credentials:
```bash
# Set credentials
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# Or use gcloud
gcloud auth application-default login

# Test connection
python -c "from google.cloud import storage; storage.Client().list_buckets()"
```

---

### Slow Reads

**Problem:** Reads are slow

**Solutions:**
1. Enable content cache:
   ```python
   backend = LocalBackend("/data", content_cache_size_mb=512)
   ```

2. Use batch reads:
   ```python
   hashes = [meta.etag for meta in file_metas]
   contents = backend.batch_read_content(hashes)
   ```

3. Check backend latency:
   ```python
   import time
   start = time.time()
   backend.read_content(hash)
   print(f"Latency: {time.time() - start:.3f}s")
   ```

---

## FAQ

### Q: Can I use multiple backends simultaneously?

**A**: Yes! That's the whole point of mounts. Mount different backends at different paths.

### Q: What happens if I unmount while files are open?

**A**: Existing file operations complete, but new operations on that path will fail until remounted.

### Q: Can I change backend without losing data?

**A**: Yes, but you need to migrate data:
```python
# Read from old
content = nx.read("/workspace/file.txt")

# Unmount old, mount new
nx.unmount("/workspace")
nx.mount("/workspace", new_backend)

# Write to new
nx.write("/workspace/file.txt", content)
```

### Q: How do I backup data across backends?

**A**: Mount source and destination, then copy:
```python
nx.mount("/local", local_backend)
nx.mount("/backup", gcs_backend)

# Copy
for file in nx.readdir("/local"):
    content = nx.read(f"/local/{file.name}")
    nx.write(f"/backup/{file.name}", content)
```

### Q: Are mounts persisted across restarts?

**A**: If using MountManager with database, yes. Otherwise, you need to recreate mounts on startup.

---

## Next Steps

- **[Content-Addressable Storage](content-addressable-storage.md)** - Deep dive into CAS
- **[Plugin System](plugin-system.md)** - Create custom backends
- **[Multi-Tenancy](multi-tenancy.md)** - Tenant isolation with mounts
- **[API Reference: Mount API](/api/mount-api/)** - Complete API docs

---

## Related Files

- Router: `src/nexus/core/router.py:1`
- Mount Manager: `src/nexus/core/mount_manager.py:1`
- Mounts Mixin: `src/nexus/core/nexus_fs_mounts.py:1`
- Backend Interface: `src/nexus/backends/backend.py:1`
- LocalBackend: `src/nexus/backends/local.py:1`
- GCSBackend: `src/nexus/backends/gcs.py:1`
- CLI: `src/nexus/cli/commands/mounts.py:1`
- FUSE: `src/nexus/fuse/mount.py:1`
- Tests: `tests/unit/core/test_router.py:1`
