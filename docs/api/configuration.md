# Configuration

← [API Documentation](README.md)

This document describes configuration options for Nexus including multi-backend support.

Nexus supports multiple storage backends that can be configured at connection time.

### Local Backend

```python
import nexus

# Use local filesystem (default)
nx = nexus.connect(config={
    "backend": "local",
    "data_dir": "./nexus-data"
})
```

### Google Cloud Storage (GCS)

```python
import nexus

# Use GCS backend
nx = nexus.connect(config={
    "backend": "gcs",
    "gcs_bucket_name": "my-bucket",
    "gcs_project_id": "my-project",  # Optional
    "gcs_credentials_path": "/path/to/credentials.json"  # Optional
})
```

Environment variables:

```bash
export NEXUS_BACKEND=gcs
export NEXUS_GCS_BUCKET_NAME=my-bucket
export NEXUS_GCS_PROJECT_ID=my-project
export NEXUS_GCS_CREDENTIALS_PATH=/path/to/credentials.json
```

---

## Configuration

### Config Dictionary

```python
config = {
    # Deployment mode
    "mode": "embedded",                # "embedded", "monolithic", or "distributed"

    # Backend configuration
    "backend": "local",                # "local" or "gcs"
    "data_dir": "./nexus-data",        # Root directory (local backend)

    # GCS backend (when backend="gcs")
    "gcs_bucket_name": "my-bucket",    # Required for GCS
    "gcs_project_id": "my-project",    # Optional
    "gcs_credentials_path": "/path",   # Optional

    # Database
    "db_path": None,                   # Custom database path (auto-generated if None)

    # Caching
    "enable_metadata_cache": True,     # In-memory metadata caching
    "cache_path_size": 512,            # Path metadata cache size
    "cache_list_size": 128,            # Directory listing cache size
    "cache_kv_size": 256,              # File metadata KV cache size
    "cache_exists_size": 1024,         # Existence check cache size
    "cache_ttl_seconds": 300,          # Cache TTL in seconds (None = no expiry)

    # Parsing
    "auto_parse": True,                # Auto-parse files on upload
    "parsers": [                       # Custom parser configurations
        {
            "module": "my_parsers.csv",
            "class": "CSVParser",
            "priority": 60,
            "enabled": True
        }
    ],

    # Permissions
    "enforce_permissions": False,      # Enable permission enforcement
    "is_admin": False,                 # Admin privileges for this instance

    # Custom namespaces
    "namespaces": [
        {
            "name": "custom",
            "readonly": False,
            "admin_only": False,
            "requires_tenant": True
        }
    ]
}

nx = nexus.connect(config=config)
```

### Config File (YAML)

```yaml
# config.yaml
mode: embedded
backend: local
data_dir: ./nexus-data

# Enable features
enable_metadata_cache: true
auto_parse: true
enforce_permissions: false

# Custom parsers
parsers:
  - module: my_parsers.csv
    class: CSVParser
    priority: 60
    enabled: true
```

```python
nx = nexus.connect(config="config.yaml")
```

### Environment Variables

```bash
# Deployment
export NEXUS_MODE=embedded
export NEXUS_BACKEND=local
export NEXUS_DATA_DIR=/var/nexus-data

# GCS backend
export NEXUS_GCS_BUCKET_NAME=my-bucket
export NEXUS_GCS_PROJECT_ID=my-project
export NEXUS_GCS_CREDENTIALS_PATH=/path/to/credentials.json

# Database (PostgreSQL)
export NEXUS_DATABASE_URL=postgresql://user:pass@localhost/nexus

# Caching
export NEXUS_ENABLE_METADATA_CACHE=true
export NEXUS_CACHE_PATH_SIZE=512
export NEXUS_CACHE_TTL_SECONDS=300

# Features
export NEXUS_AUTO_PARSE=true
export NEXUS_ENFORCE_PERMISSIONS=false
export NEXUS_IS_ADMIN=false

# Custom parsers (comma-separated)
export NEXUS_PARSERS="my_parsers.csv:CSVParser:60,my_parsers.log:LogParser:50"
```

```python
# Auto-detects from environment
nx = nexus.connect()
```

### Configuration Object

```python
from nexus import NexusConfig

config = NexusConfig(
    mode="embedded",
    data_dir="./nexus-data"
)

nx = nexus.connect(config=config)
```

---

## Multi-Backend Support

Nexus supports mounting multiple storage backends to different paths, enabling scenarios like hot/cold storage tiering, multi-cloud strategies, and gradual migrations.

### YAML Configuration

Create a `nexus.yaml` file with multiple backend mounts:

```yaml
# Primary backend (mounted at root /)
mode: embedded
backend: local
data_dir: ./nexus-local

# Additional backend mounts
backends:
  # Local archive storage (read-only)
  - name: local-archive
    type: local
    mount_point: /archives
    data_dir: ./nexus-archives
    priority: 10
    readonly: true

  # Google Cloud Storage for cold storage
  - name: gcs-cold
    type: gcs
    mount_point: /cloud
    bucket_name: my-archive-bucket
    project_id: my-gcp-project
    priority: 10
    readonly: false

# Shared metadata database
db_path: ./nexus-multi.db
```

### Backend Configuration Options

Each backend mount supports:

- **name**: Backend identifier (for reference)
- **type**: Backend type (`local` or `gcs`)
- **mount_point**: Virtual path prefix (e.g., `/archives`, `/cloud`)
- **priority**: Priority for overlapping mounts (higher = preferred, default: 0)
- **readonly**: Whether mount is readonly (default: false)

#### Local Backend Options

- **data_dir**: Path to local data directory

#### GCS Backend Options

- **bucket_name**: GCS bucket name (required)
- **project_id**: GCP project ID (optional, inferred from credentials)
- **credentials_path**: Path to credentials JSON (optional, uses ADC if not provided)

### Usage

```bash
# Set config environment variable
export NEXUS_CONFIG=./nexus-multi.yaml

# Write to primary backend (/)
nexus write /workspace/file.txt "local data"

# Write to cloud backend (/cloud)
nexus write /cloud/backup.txt "cloud data"

# Read from archives (read-only)
nexus cat /archives/old-data.txt

# List files across backends
nexus ls /workspace
nexus ls /cloud
nexus ls /archives
```

### Python API

```python
import nexus

# Connect with multi-backend config
nx = nexus.connect(config="./nexus-multi.yaml")

# Access different backends
nx.write("/workspace/local-file.txt", b"primary backend")
nx.write("/cloud/cloud-file.txt", b"cloud backend")
content = nx.read("/archives/archived.txt")

nx.close()
```

### Path Routing

Nexus uses longest-prefix matching to route paths to backends:

```yaml
# Configuration
backend: local  # Mounted at /
data_dir: ./local

backends:
  - mount_point: /cloud
    type: gcs
    bucket_name: my-bucket
```

Path routing:
- `/workspace/file.txt` → Local Backend
- `/cloud/backup.txt` → GCS Backend
- `/cloud/subfolder/file.txt` → GCS Backend

When multiple backends match a path, priority determines the winner (higher priority wins).

### Common Use Cases

#### 1. Hot/Cold Storage Tiering

```yaml
# Hot storage - local SSD for fast access
backend: local
data_dir: ./nexus-hot

# Cold storage - cloud archive (read-only)
backends:
  - name: cold-archive
    type: gcs
    mount_point: /archives
    bucket_name: my-cold-storage
    readonly: true
```

#### 2. Multi-Cloud Strategy

```yaml
backends:
  - name: primary-gcs
    type: gcs
    mount_point: /primary
    bucket_name: main-bucket

  - name: backup
    type: local
    mount_point: /backup
    data_dir: /mnt/backup-data
```

#### 3. Gradual Migration

```yaml
backends:
  # Legacy local storage (read-only during migration)
  - name: legacy
    type: local
    mount_point: /legacy
    data_dir: /old/nexus/data
    readonly: true

  # New cloud deployment
  - name: cloud
    type: gcs
    mount_point: /
    bucket_name: new-deployment
```

### Remote Mode Support

Multi-backend mounting is currently supported in **embedded mode only**. When using Nexus in remote mode, the multi-backend configuration must be set up on the server side—clients will transparently access all configured backends through the server.

### Examples

See working examples in:
- **Config Files**: `examples/config/multi_backend.yaml`, `examples/config/hot_cold_storage.yaml`
- **Demo Script**: `examples/script_demo/multi_backend_demo.sh`
- **Python Demo**: `examples/py_demo/multi_backend_usage_demo.py`

---

## See Also

- [Core API](core-api.md) - Connection methods
- [Getting Started](getting-started.md) - Quick start
- [Mounts](mounts.md) - Dynamic mounts

## Next Steps

1. Choose your [backend storage](configuration.md#multi-backend-support)
2. Set up [environment variables](#environment-variables)
3. Create a [config file](#config-file-yaml)
