# Multi-Backend Storage

**⏱️ Time: 20 minutes** | **📊 Difficulty: Intermediate** | **🎯 Goal: Master hybrid storage with local + cloud backends**

## What You'll Learn

By the end of this guide, you'll understand how to:

- ✅ Start Nexus server with local backend
- ✅ Add GCS backend dynamically at runtime
- ✅ Route different paths to different backends
- ✅ Implement hybrid storage patterns (hot/cold data)
- ✅ Optimize costs with tiered storage
- ✅ Build production-ready multi-cloud architectures

## Prerequisites

- Nexus installed (`pip install nexus-ai-fs`)
- GCS bucket and service account credentials (optional but recommended)
- Basic understanding of Nexus file operations

## Why Multi-Backend Storage?

Traditional approaches force you to choose ONE storage system:

❌ **Traditional Problems:**
- Store everything in expensive cloud storage OR slow local disk
- Rewrite code to switch between storage providers
- Manual migration when requirements change
- Vendor lock-in

✅ **Nexus Multi-Backend Solution:**
- **Mix-and-match**: Local for hot data, GCS for archives
- **Transparent routing**: Same API, different backends
- **Cost optimization**: Right storage tier for each use case
- **Zero vendor lock-in**: Switch backends without code changes

### Real-World Use Cases

| Use Case | Storage Strategy |
|----------|------------------|
| **AI Training** | `/datasets/raw` → GCS (durable)<br>`/cache/processed` → Local (fast) |
| **Document Processing** | `/inbox` → Local (temporary)<br>`/archive` → GCS (long-term) |
| **Multi-Region Apps** | `/us-west` → GCS us-west1<br>`/eu` → GCS europe-west1 |
| **Cost Optimization** | `/hot` → Local SSD<br>`/warm` → Local HDD<br>`/cold` → GCS Coldline |

---

## Part 1: Server Setup

### Step 1: Start Nexus Server

Start Nexus server with database authentication:

```bash
# First-time setup: Initialize server with admin user
nexus serve --auth-type database --init --port 8765

# Output:
# ✓ Admin user created: admin
# ✓ API key: nxk_abc123def456...
# Save this API key - you'll need it for client connections!
```

**Save the API key** - this is the only time it will be displayed!

For subsequent starts (after initialization):

```bash
# Restart server (already initialized)
nexus serve --auth-type database --port 8765
```

### Step 2: Create Admin User and API Key

```bash
# Create admin user (if needed)
nexus admin create-user admin \
  --name "Admin User" \
  --email admin@company.com

# Generate API key for admin
nexus admin create-user-key admin
# Output: nxk_abc123def456...
```

---

## Part 2: Working with Multiple Backends

### Step 1: Connect Client to Server

Use the recommended `nexus.connect()` pattern which auto-detects remote mode:

```python
import nexus

# Connect to server (auto-detects remote mode from NEXUS_URL)
# export NEXUS_URL=http://localhost:8765
# export NEXUS_API_KEY=nxk_abc123...
nx = nexus.connect()

# Or connect explicitly
nx = nexus.connect(config={
    "url": "http://localhost:8765",
    "api_key": "nxk_abc123..."
})
```

Alternatively, use `RemoteNexusFS` directly:

```python
from nexus import RemoteNexusFS

# Direct remote connection
nx = RemoteNexusFS(
    server_url="http://localhost:8765",
    api_key="nxk_abc123..."
)
```

### Step 2: Add GCS Backend Dynamically

Now add a GCS backend for archival storage:

```python
# Add GCS mount for archives
gcs_mount_id = nx.mount_manager.add_mount(
    mount_point="/archives",
    backend_type="gcs",
    backend_config={
        "bucket": "my-nexus-archives",
        "project_id": "my-gcp-project"
        # credentials_path: "/path/to/service-account.json"  # Optional
    },
    priority=10,
    readonly=False
)
print(f"✓ GCS backend mounted at /archives (ID: {gcs_mount_id})")

# Add second GCS mount for shared datasets (read-only)
dataset_mount_id = nx.mount_manager.add_mount(
    mount_point="/datasets",
    backend_type="gcs",
    backend_config={
        "bucket": "company-ml-datasets",
        "project_id": "my-gcp-project"
    },
    priority=20,
    readonly=True  # Prevent accidental modifications
)
print(f"✓ Dataset backend mounted at /datasets (ID: {dataset_mount_id})")
```

### Step 3: List Available Mounts

```python
# List all mounts
mounts = nx.list_mounts()
for mount in mounts:
    print(f"📂 {mount.mount_point}: {mount.backend_type} "
          f"(priority={mount.priority}, readonly={mount.readonly})")

# Output:
# 📂 /workspace: local (priority=0, readonly=False)
# 📂 /archives: gcs (priority=10, readonly=False)
# 📂 /datasets: gcs (priority=20, readonly=True)
```

### Step 4: Write to Different Backends

The **same API** works across all backends - Nexus routes automatically:

```python
# Write to default local backend
nx.write("/workspace/active-task.txt", b"Processing...")
print("✓ Written to: Local backend")

# Write to GCS archive (durable, cost-effective)
nx.write("/archives/2024/report.pdf", b"PDF content...")
print("✓ Written to: GCS (gs://my-nexus-archives/...)")

# Try to write to read-only dataset (will fail)
try:
    nx.write("/datasets/new-data.csv", b"data")
except Exception as e:
    print(f"✗ Cannot write to read-only mount: {e}")

# Read works from any backend
data = nx.read("/datasets/reference/model-weights.bin")
print(f"✓ Read {len(data)} bytes from GCS dataset")
```

**Key Insight:** Your code doesn't know or care where data is stored. Nexus handles routing automatically based on path.

### Step 5: Path Routing and Priority

Understanding how Nexus routes paths to backends:

```python
# Example: Add overlapping mount with higher priority
nx.mount_manager.add_mount(
    mount_point="/workspace/shared",
    backend_type="gcs",
    backend_config={"bucket": "team-shared", "project_id": "my-project"},
    priority=10  # Higher than default /workspace (priority=0)
)

# Routing examples:
nx.write("/workspace/file.txt", b"data")
# → Matches: /workspace (local, priority=0)
# ✓ Routed to: local backend

nx.write("/workspace/shared/team-doc.txt", b"data")
# → Matches: /workspace (local, priority=0)
#            /workspace/shared (gcs, priority=10)
# ✓ Routed to: gcs backend (longer prefix + higher priority)

# Get routing information for a path
mount = nx.get_mount_info("/workspace/shared")
if mount:
    print(f"Path /workspace/shared routes to: {mount.backend_type}")
```

**Routing Algorithm:**
1. Find all mounts with matching path prefix
2. Sort by: **priority (DESC)** → **prefix length (DESC)**
3. Return first match

---

## Part 3: Production Patterns

### Pattern 1: Hot/Cold Data Tiering

Optimize costs by storing frequently-accessed data locally, archive to cloud:

```python
from datetime import datetime, timedelta

def tier_old_files(nx, hot_path: str, cold_path: str, days_threshold: int = 30):
    """Move old files from hot (local) to cold (GCS) storage"""

    cutoff_date = datetime.now() - timedelta(days=days_threshold)
    files_moved = 0

    # List files in hot storage
    for entry in nx.list(hot_path, recursive=True):
        if not entry['is_directory']:
            file_path = entry['path']

            # Get file metadata
            metadata = nx.stat(file_path)
            created_at = datetime.fromisoformat(metadata['created_at'])

            # Move to cold storage if older than threshold
            if created_at < cutoff_date:
                # Construct cold storage path
                rel_path = file_path.replace(hot_path, "")
                cold_file = f"{cold_path}{rel_path}"

                # Copy to cold storage
                content = nx.read(file_path)
                nx.write(cold_file, content)

                # Delete from hot storage
                nx.remove(file_path)

                files_moved += 1
                print(f"📦 Archived: {file_path} → {cold_file}")

    print(f"✓ Moved {files_moved} files to cold storage")
    return files_moved

# Example: Archive old workspace files to GCS
tier_old_files(nx,
    hot_path="/workspace/processed",
    cold_path="/archives/workspace-archive",
    days_threshold=7  # Move files older than 7 days
)
```

### Pattern 2: Content Deduplication Across Backends

Nexus automatically deduplicates content **within each backend** using SHA-256 hashing:

```python
# Same content written to different paths
content = b"Shared configuration data"

# Write to local backend
nx.write("/workspace/config.yaml", content)
nx.write("/workspace/backup/config.yaml", content)  # Deduplicated!

# Write to GCS backend
nx.write("/archives/configs/v1.yaml", content)
nx.write("/archives/configs/v2.yaml", content)  # Deduplicated!

# Each backend stores content ONCE, both paths reference same content hash
print("✓ 4 paths, 2 backends, only 2 physical copies (one per backend)")

# Note: Deduplication is per-backend (local + GCS each store one copy)
# This is by design - each backend is independent
```

### Pattern 3: Multi-Tenant Storage Isolation

Each tenant gets their own backend mount:

```python
def provision_tenant_storage(nx, tenant_id: str, gcs_bucket: str):
    """Provision isolated storage for a new tenant"""

    # Create tenant-specific mount
    mount_id = nx.mount_manager.add_mount(
        mount_point=f"/tenants/{tenant_id}",
        backend_type="gcs",
        backend_config={
            "bucket": gcs_bucket,
            "prefix": f"tenant-{tenant_id}/",  # Bucket prefix isolation
            "project_id": "my-gcp-project"
        },
        priority=20
    )

    # Create tenant workspace structure
    nx.mkdir(f"/tenants/{tenant_id}/documents")
    nx.mkdir(f"/tenants/{tenant_id}/uploads")
    nx.mkdir(f"/tenants/{tenant_id}/exports")

    print(f"✓ Tenant {tenant_id} provisioned with mount {mount_id}")
    return mount_id

# Provision storage for new tenants
provision_tenant_storage(nx, "acme-corp", "nexus-prod-storage")
provision_tenant_storage(nx, "globex", "nexus-prod-storage")

# Each tenant's data is isolated
nx.write("/tenants/acme-corp/documents/contract.pdf", b"...")
nx.write("/tenants/globex/documents/proposal.pdf", b"...")

# Tenants CANNOT access each other's data (enforced by path isolation + ReBAC)
```

### Pattern 4: Hybrid Cloud Bursting

Start with local storage, burst to cloud when capacity is reached:

```python
def smart_write(nx, path: str, content: bytes, local_limit_gb: float = 100.0):
    """Write to local if space available, otherwise use cloud"""

    # Check local storage usage
    local_usage = get_local_storage_usage()  # Custom function

    if local_usage < local_limit_gb:
        # Write to fast local storage
        local_path = f"/workspace{path}"
        nx.write(local_path, content)
        print(f"✓ Written to local: {local_path}")
    else:
        # Burst to cloud storage
        cloud_path = f"/archives{path}"
        nx.write(cloud_path, content)
        print(f"☁️  Burst to cloud: {cloud_path}")

def get_local_storage_usage() -> float:
    """Get local storage usage in GB"""
    import shutil
    stat = shutil.disk_usage("/var/nexus/data")
    return (stat.total - stat.free) / (1024**3)

# Usage
for i in range(1000):
    smart_write(nx, f"/data/file-{i}.bin", b"x" * 1024 * 1024)  # 1MB files
```

---

## Part 4: GCS Backend Setup

### Prerequisites

1. **GCS Bucket**: Create a bucket in Google Cloud Console
2. **Service Account**: Create service account with `Storage Object Admin` role
3. **Credentials**: Download JSON key file

### Step 1: Create GCS Bucket

```bash
# Using gcloud CLI (or via console)
gsutil mb -l us-west1 gs://my-nexus-storage
gsutil mb -l us-west1 gs://my-nexus-archives

# Set lifecycle policy for cost optimization (optional)
gsutil lifecycle set lifecycle-policy.json gs://my-nexus-archives
```

Example `lifecycle-policy.json`:
```json
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
        "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}
      },
      {
        "action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
        "condition": {"age": 90, "matchesStorageClass": ["NEARLINE"]}
      }
    ]
  }
}
```

### Step 2: Configure Service Account

```bash
# Set credentials via environment variable (recommended for server)
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"

# Then add mount via Python API (shown in Part 2)
```

### Step 3: Test GCS Connection

```python
import nexus

nx = nexus.connect(config={
    "url": "http://localhost:8765",
    "api_key": "nxk_abc123..."
})

# Add GCS mount
nx.mount_manager.add_mount(
    mount_point="/archives",
    backend_type="gcs",
    backend_config={
        "bucket": "my-nexus-archives",
        "project_id": "my-gcp-project"
    },
    priority=10
)

# Test write to GCS
test_path = "/archives/test/hello.txt"
nx.write(test_path, b"Hello from Nexus!")
print(f"✓ Written to GCS: {test_path}")

# Test read from GCS
content = nx.read(test_path)
print(f"✓ Read from GCS: {content.decode()}")

# Verify in GCS bucket
# gsutil ls gs://my-nexus-archives/cas/
# Should see content-addressed files
```

---

## Part 5: Local-Only Multi-Backend (No GCS)

If you don't have GCS set up, you can still learn multi-backend concepts with multiple local backends:

```python
import nexus

# Start server with default local backend
# nexus serve --auth-type database --init --port 8765

nx = nexus.connect(config={
    "url": "http://localhost:8765",
    "api_key": "nxk_abc123..."
})

# Add multiple local backends (simulating different storage tiers)
# Fast SSD storage
nx.mount_manager.add_mount(
    mount_point="/fast",
    backend_type="local",
    backend_config={"data_dir": "/tmp/nexus/fast-ssd"},
    priority=20
)

# Slow but large storage (simulating archive)
nx.mount_manager.add_mount(
    mount_point="/archive",
    backend_type="local",
    backend_config={"data_dir": "/tmp/nexus/archive-hdd"},
    priority=10
)

# Temporary scratch space
nx.mount_manager.add_mount(
    mount_point="/scratch",
    backend_type="local",
    backend_config={"data_dir": "/tmp/nexus/scratch"},
    priority=5
)

# Use exactly the same API with multiple local backends
nx.write("/fast/active-model.bin", model_data)
nx.write("/archive/2024-q1-data.tar.gz", archive_data)
nx.write("/scratch/temp-results.json", temp_data)

# Same multi-backend patterns work with all local storage!
```

---

## Common Patterns Summary

| Pattern | Use Case | Implementation |
|---------|----------|----------------|
| **Hot/Cold Tiering** | Cost optimization | Local for recent files, GCS for archives |
| **Multi-Region** | Low latency | Regional GCS buckets, route by user location |
| **Hybrid Burst** | Capacity management | Local until full, then cloud |
| **Tenant Isolation** | Multi-tenancy | Separate GCS bucket prefix per tenant |
| **Read-Only Datasets** | Shared resources | Mount GCS bucket as readonly |
| **Temporary Mounts** | Job-specific storage | Dynamic mount + cleanup after job |

---

## Troubleshooting

### Issue: "Backend not found for path /archives/file.txt"

**Cause:** Mount not configured.

**Solution:**
```python
# Verify mounts are configured
mounts = nx.list_mounts()
for m in mounts:
    print(f"{m.mount_point}: {m.backend_type}")

# Add missing mount
nx.mount_manager.add_mount(
    mount_point="/archives",
    backend_type="gcs",
    backend_config={"bucket": "my-bucket", "project_id": "my-project"},
    priority=10
)
```

### Issue: GCS authentication errors

**Cause:** Invalid or missing credentials.

**Solution:**
```bash
# Verify credentials file exists and is valid
cat /path/to/service-account.json

# Test gcloud authentication
gcloud auth application-default login

# Or set explicit path
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
```

### Issue: Write fails to GCS mount

**Cause:** Insufficient permissions on service account.

**Solution:**
```bash
# Grant Storage Object Admin role
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:SERVICE_ACCOUNT_EMAIL" \
  --role="roles/storage.objectAdmin"
```

### Issue: Slow GCS operations

**Cause:** Network latency or bucket location mismatch.

**Solution:**
- Create bucket in same region as your server
- Use regional buckets, not multi-regional
- Consider adding local caching layer

---

## What's Next?

You've mastered multi-backend storage! Here's how to continue:

### Immediate Next Steps

1. **[Multi-Tenant SaaS](multi-tenant-saas.md)** - Build production multi-tenant apps with backend isolation
2. **[Team Collaboration](team-collaboration.md)** - Multi-user permissions with shared storage
3. **[Workflow Automation](workflow-automation.md)** - Automate data tiering with workflows

### Production Checklist

Before deploying multi-backend setup to production:

- [ ] Configure authentication and API keys
- [ ] Set up GCS service account with minimal required permissions
- [ ] Test failover scenarios (backend unavailable, network issues)
- [ ] Implement monitoring for backend health and latency
- [ ] Configure lifecycle policies for cost optimization
- [ ] Document backend topology and routing rules
- [ ] Set up backup strategy for each backend
- [ ] Test disaster recovery procedures

### Advanced Topics

- **Custom Backends**: Implement `Backend` interface for S3, Azure, PostgreSQL
- **Backend Replication**: Sync data across multiple backends for redundancy
- **Caching Strategies**: Multi-layer caching (memory → local → cloud)
- **Backend Migration**: Move data between backends without downtime

---

## Complete Example: Production Setup

```python
"""
Production multi-backend setup with:
- Local storage for hot data
- GCS for durable archival
- Automatic tiering
- Multi-tenant isolation
"""

import nexus
from datetime import datetime, timedelta

# Connect to server
nx = nexus.connect(config={
    "url": "https://nexus.company.com",
    "api_key": "nxk_prod_key_..."
})

# Add GCS archive backend
nx.mount_manager.add_mount(
    mount_point="/archives",
    backend_type="gcs",
    backend_config={
        "bucket": "company-archives",
        "project_id": "prod-project"
    },
    priority=10
)

# Add GCS cold storage
nx.mount_manager.add_mount(
    mount_point="/cold",
    backend_type="gcs",
    backend_config={
        "bucket": "company-coldline",
        "project_id": "prod-project"
    },
    priority=5
)

class StorageManager:
    def __init__(self, nx):
        self.nx = nx
        self.hot_path = "/workspace"
        self.warm_path = "/archives"
        self.cold_path = "/cold"

    def write_with_tiering(self, path: str, content: bytes):
        """Write to hot storage, automatically tier later"""
        hot_file = f"{self.hot_path}{path}"
        self.nx.write(hot_file, content)

        # Tag for future tiering
        self.nx.set_metadata(hot_file, {
            "tier": "hot",
            "accessed_at": datetime.now().isoformat()
        })

        return hot_file

    def tier_data(self):
        """Background job: Tier old data from hot → warm → cold"""
        now = datetime.now()

        # Hot → Warm (after 7 days)
        for entry in self.nx.list(self.hot_path, recursive=True):
            if entry['is_directory']:
                continue

            meta = self.nx.get_metadata(entry['path'])
            accessed = datetime.fromisoformat(meta.get('accessed_at', now.isoformat()))

            if now - accessed > timedelta(days=7):
                self._move_to_tier(entry['path'], self.hot_path, self.warm_path, "warm")

        # Warm → Cold (after 90 days)
        for entry in self.nx.list(self.warm_path, recursive=True):
            if entry['is_directory']:
                continue

            meta = self.nx.get_metadata(entry['path'])
            accessed = datetime.fromisoformat(meta.get('accessed_at', now.isoformat()))

            if now - accessed > timedelta(days=90):
                self._move_to_tier(entry['path'], self.warm_path, self.cold_path, "cold")

    def _move_to_tier(self, path: str, from_tier: str, to_tier: str, tier_name: str):
        """Move file between tiers"""
        rel_path = path.replace(from_tier, "")
        new_path = f"{to_tier}{rel_path}"

        # Copy to new tier
        content = self.nx.read(path)
        self.nx.write(new_path, content)

        # Update metadata
        self.nx.set_metadata(new_path, {
            "tier": tier_name,
            "accessed_at": datetime.now().isoformat(),
            "original_path": path
        })

        # Remove from old tier
        self.nx.remove(path)
        print(f"✓ Tiered: {path} → {new_path}")

# Usage
storage = StorageManager(nx)

# Application writes always go to hot storage
storage.write_with_tiering("/data/model-output.bin", model_data)
storage.write_with_tiering("/data/user-upload.pdf", pdf_content)

# Run tiering job (would be scheduled via cron/workflow)
storage.tier_data()

print("✓ Production storage manager running")
print("  - Hot tier: Local (/workspace)")
print("  - Warm tier: GCS Standard (/archives)")
print("  - Cold tier: GCS Coldline (/cold)")
```

---

## Key Takeaways

1. **Dynamic Mount Management**: Add backends at runtime via `mount_manager.add_mount()`
2. **Transparent Routing**: Path-based routing handled automatically
3. **Cost Optimization**: Right storage tier for each use case
4. **No Vendor Lock-in**: Switch backends without code changes
5. **Production-Ready**: Multi-region, multi-tenant, automatic tiering

**Next:** [Multi-Tenant SaaS →](multi-tenant-saas.md)
