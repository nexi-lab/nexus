# Advanced Usage

← [API Documentation](README.md)

This document describes advanced patterns and usage examples for Nexus.

### Batch Operations

```python
import nexus

nx = nexus.connect()

# Batch write
files = {
    "/data/file1.txt": b"content1",
    "/data/file2.txt": b"content2",
    "/data/file3.txt": b"content3",
}

for path, content in files.items():
    nx.write(path, content)

# Batch read
contents = {}
for path in nx.list(prefix="/data"):
    contents[path] = nx.read(path)

nx.close()
```

### Working with JSON

```python
import json
import nexus

nx = nexus.connect()

# Write JSON
data = {"users": [{"id": 1, "name": "Alice"}]}
nx.write("/data/users.json", json.dumps(data, indent=2).encode())

# Read JSON
content = nx.read("/data/users.json")
data = json.loads(content)
print(data["users"])

nx.close()
```

### Path Patterns

```python
# Valid paths
nx.write("/file.txt", b"content")              # ✅
nx.write("/path/to/nested/file.txt", b"data")  # ✅
nx.write("/documents/2025/report.pdf", b"pdf") # ✅

# Invalid paths
nx.write("no-slash.txt", b"content")           # ❌ No leading slash
nx.write("/path/../file.txt", b"content")      # ❌ Contains '..'
nx.write("/path\nfile.txt", b"content")        # ❌ Contains newline
```

### File Size Limits

```python
# No explicit file size limits in v0.1.0
# Limited by:
# - Available disk space
# - SQLite database limits (2TB max)
# - Python memory for in-memory operations

# For large files, read in chunks (future enhancement)
```

### Performance Tips

```python
# 1. Use batch operations instead of individual calls
files_to_write = [...]
for path, content in files_to_write:
    nx.write(path, content)  # Each call is a transaction

# 2. Use list() with prefix for filtering
docs = nx.list(prefix="/documents")  # Fast, uses index
all_files = nx.list()
docs = [f for f in all_files if f.startswith("/documents")]  # Slower

# 3. Check existence before read
if nx.exists(path):
    content = nx.read(path)
# Instead of try/except for normal flow

# 4. Use context manager for automatic cleanup
with nexus.connect() as nx:
    # Operations
    pass
# Automatically closed
```

---

## Multi-Backend Patterns

### Programmatic Mount Management

```python
from nexus import NexusFS, LocalBackend, GCSBackend

# Create primary backend
local = LocalBackend(root_path="./nexus-local")
nx = NexusFS(backend=local, db_path="./nexus.db")

# Add cloud backend mount at runtime
gcs = GCSBackend(bucket_name="my-bucket", project_id="my-project")
nx.router.add_mount("/cloud", gcs, priority=10)

# Use both backends transparently
nx.write("/workspace/local.txt", b"local data")
nx.write("/cloud/remote.txt", b"cloud data")

# List files across backends
local_files = nx.list("/workspace")
cloud_files = nx.list("/cloud")

nx.close()
```

### Dynamic User-Specific Mounts

```python
import nexus
from nexus.core.permissions import OperationContext

def setup_user_workspace(user_id: str):
    """Set up isolated workspace for a user"""
    nx = nexus.connect()

    # Create user context
    ctx = OperationContext(user=user_id)

    # Add user-specific mount
    mount_id = nx.add_mount(
        mount_point=f"/personal/{user_id}",
        backend_type="gcs",
        backend_config={
            "bucket": f"{user_id}-personal-bucket",
            "project_id": "my-project"
        },
        priority=10
    )

    # User can now access their personal space
    nx.write(f"/personal/{user_id}/notes.txt", b"My notes", context=ctx)

    return mount_id

# Setup mounts for multiple users
alice_mount = setup_user_workspace("alice")
bob_mount = setup_user_workspace("bob")
```

### Hot/Cold Storage with Auto-Archival

```python
import nexus
from datetime import datetime, timedelta

def archive_old_files(nx, days_old: int = 90):
    """Move old files from hot to cold storage"""

    # List all files in hot storage
    hot_files = nx.list("/workspace", recursive=True)
    cutoff_date = datetime.now() - timedelta(days=days_old)

    archived_count = 0
    for file_path in hot_files:
        # Get file metadata
        info = nx.stat(file_path)

        # Check if file is old enough
        if info['modified_at'] < cutoff_date:
            # Read from hot storage
            content = nx.read(file_path)

            # Write to cold storage (archive mount)
            archive_path = file_path.replace("/workspace", "/archives")
            nx.write(archive_path, content)

            # Delete from hot storage
            nx.delete(file_path)
            archived_count += 1

    return archived_count

# Connect with multi-backend config
nx = nexus.connect(config="./nexus-multi.yaml")

# Archive old files
count = archive_old_files(nx, days_old=90)
print(f"Archived {count} files to cold storage")

nx.close()
```

### Read-Only Data Sharing

```python
import nexus

# Setup: Admin creates read-only mount for shared resources
def setup_shared_resources():
    nx = nexus.connect()

    # Add read-only mount for public datasets
    nx.add_mount(
        mount_point="/shared/datasets",
        backend_type="gcs",
        backend_config={
            "bucket": "public-datasets-bucket"
        },
        priority=5,
        readonly=True  # Prevents accidental modifications
    )

    return nx

# Usage: Users can read but not modify
nx = setup_shared_resources()

# This works
data = nx.read("/shared/datasets/public-data.csv")

# This raises PermissionError
try:
    nx.write("/shared/datasets/new-file.txt", b"data")
except PermissionError:
    print("Cannot write to read-only mount")

nx.close()
```

---

## Remote Server Examples

For complete examples of using Nexus with a remote server, see:

- **Python SDK**: [`examples/python/advanced_usage_demo.py`](../../examples/python/advanced_usage_demo.py)
- **CLI**: [`examples/cli/advanced_usage_demo.sh`](../../examples/cli/advanced_usage_demo.sh)
- **Server Setup**: [`scripts/init-nexus-with-auth.sh`](../../scripts/init-nexus-with-auth.sh)

### Quick Start - Remote Server

**1. Setup and start server:**
```bash
# Initialize server with authentication
./scripts/init-nexus-with-auth.sh

# In terminal 1: Load credentials (server starts automatically)
source .nexus-admin-env
```

**2. Run examples (in a new terminal):**
```bash
# Terminal 2: Load credentials and run examples
source .nexus-admin-env

# Python example
python examples/python/advanced_usage_demo.py

# CLI example
./examples/cli/advanced_usage_demo.sh
```

### Python Remote Connection

```python
from nexus.remote.client import RemoteNexusFS
import os

# Connect to remote server with authentication
nx = RemoteNexusFS(
    server_url=os.environ['SERVER_URL'],    # http://localhost:8080
    api_key=os.environ['NEXUS_API_KEY']     # sk-admin_...
)

# Create workspace
nx.mkdir("/workspace/my-project", parents=True)

# Write file
nx.write("/workspace/my-project/data.txt", b"Hello from remote!")

# Read file
content = nx.read("/workspace/my-project/data.txt")
print(content.decode())  # "Hello from remote!"

# List files
files = nx.list("/workspace/my-project")
print(files)

nx.close()
```

### CLI Remote Usage

```bash
# Set environment variables (or use: source .nexus-admin-env)
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="sk-admin_..."

# CLI automatically uses NEXUS_URL and NEXUS_API_KEY from environment
nexus mkdir /workspace/my-project
nexus write /workspace/my-project/data.txt "Hello from CLI!"
nexus cat /workspace/my-project/data.txt
nexus ls /workspace/my-project
```

**Note**: The `--remote-url` flag is optional - the CLI automatically uses `NEXUS_URL` if set!

See [Getting Started Guide](../getting-started/quickstart.md) for detailed server setup and authentication.

---

## Examples

### Complete Application (Embedded Mode)

```python
import nexus
import json
from datetime import datetime
from nexus.core.permissions import OperationContext

def main():
    # Initialize with configuration (embedded mode - no server)
    nx = nexus.connect(config={
        "backend": "local",
        "data_dir": "./app-data",
        "enable_metadata_cache": True,
        "auto_parse": True
    })

    try:
        # Create operation context for a user
        ctx = OperationContext(
            user="alice",
            groups=["team-engineering"],
            is_admin=False
        )

        # Store application config with versioning
        config = {
            "app_name": "MyApp",
            "version": "1.0.0",
            "created_at": datetime.now().isoformat()
        }
        metadata = nx.write(
            "/workspace/config/app.json",
            json.dumps(config, indent=2).encode(),
            context=ctx
        )
        print(f"Config written (etag: {metadata['etag'][:8]}...)")

        # Batch write user data (4x faster!)
        users = [
            ("/workspace/users/alice.json", json.dumps({"name": "Alice", "role": "admin"}).encode()),
            ("/workspace/users/bob.json", json.dumps({"name": "Bob", "role": "user"}).encode()),
            ("/workspace/users/charlie.json", json.dumps({"name": "Charlie", "role": "user"}).encode()),
        ]
        results = nx.write_batch(users, context=ctx)
        print(f"Batch wrote {len(results)} user files")

        # List all users
        user_files = nx.list("/workspace/users")
        print(f"Users: {user_files}")

        # Search for admin users using grep
        admin_matches = nx.grep(r'"role":\s*"admin"', file_pattern="**/*.json")
        print(f"Found {len(admin_matches)} admin users")

        # Read config with metadata
        result = nx.read("/workspace/config/app.json", context=ctx, return_metadata=True)
        config_data = json.loads(result['content'])
        print(f"App: {config_data['app_name']} v{config_data['version']}")
        print(f"Version: {result['version']}, Size: {result['size']} bytes")

        # Create a workspace snapshot
        snapshot = nx.workspace_snapshot(
            agent_id="alice",
            description="Initial setup",
            tags=["v1.0.0", "baseline"]
        )
        print(f"Created snapshot {snapshot['snapshot_number']}")

        # Cleanup old data
        if nx.exists("/workspace/temp/cache.dat"):
            nx.delete("/workspace/temp/cache.dat")

        # List all versions of config file
        versions = nx.list_versions("/workspace/config/app.json")
        print(f"Config has {len(versions)} versions")

    finally:
        nx.close()

if __name__ == "__main__":
    main()
```


## See Also

- [File Operations](file-operations.md) - Basic operations
- [Configuration](configuration.md) - Advanced configuration
- [Error Handling](error-handling.md) - Error patterns

## Next Steps

1. Review [examples](#complete-application)
2. Optimize with [performance tips](#performance-tips)
3. Explore [CLI workflows](cli-reference.md)
