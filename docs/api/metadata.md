# Metadata Management

← [API Documentation](README.md)

This document describes metadata export, import, and batch operations.

Nexus provides APIs for exporting and importing metadata, useful for backup, migration, and disaster recovery scenarios.

### export_metadata()

Export file metadata to a JSONL (JSON Lines) file for backup and migration.

```python
def export_metadata(
    output_path: str | Path,
    filter: ExportFilter | None = None,
    prefix: str = ""  # Deprecated
) -> int
```

**Parameters:**
- `output_path` (str | Path): Path to output JSONL file
- `filter` (ExportFilter, optional): Export filter options
  - `path_prefix`: Only export files with this prefix
  - `after_time`: Only export files modified after this timestamp
  - `tenant_id`: Only export files for specific tenant
  - `include_deleted`: Include deleted files (default: False)
- `prefix` (str, optional): **Deprecated** - Use filter.path_prefix instead

**Returns:**
- `int`: Number of files exported

**Examples:**

```python
# Export all metadata
count = nx.export_metadata("backup.jsonl")
print(f"Exported {count} files")

# Export with filters
from nexus.core.export_import import ExportFilter
from datetime import datetime

filter = ExportFilter(
    path_prefix="/workspace",
    after_time=datetime(2024, 1, 1),
    tenant_id="acme-corp"
)
count = nx.export_metadata("backup.jsonl", filter=filter)

# Export recent changes only
filter = ExportFilter(after_time=datetime(2025, 1, 1))
count = nx.export_metadata("recent.jsonl", filter=filter)
```

**Output Format (JSONL):**

Each line is a JSON object:

```json
{"path": "/file.txt", "backend_name": "local", "physical_path": "cas/abc123...", "size": 1024, "etag": "abc123...", "mime_type": "text/plain", "created_at": "2025-01-15T10:30:00Z", "modified_at": "2025-01-15T10:30:00Z", "version": 1, "custom_metadata": {"key": "value"}}
```

---

### import_metadata()

Import metadata from a JSONL file.

**IMPORTANT:** This only imports metadata records, not actual file content. The content must already exist in the CAS storage (matched by content hash).

```python
def import_metadata(
    input_path: str | Path,
    options: ImportOptions | None = None,
    overwrite: bool = False,  # Deprecated
    skip_existing: bool = True  # Deprecated
) -> ImportResult
```

**Parameters:**
- `input_path` (str | Path): Path to input JSONL file
- `options` (ImportOptions, optional): Import options
  - `conflict_mode`: How to handle conflicts - "skip", "overwrite", "auto", or "error" (default: "skip")
  - `dry_run`: If True, simulate import without changes (default: False)
  - `preserve_ids`: Preserve original IDs if possible (default: True)
- `overwrite` (bool, optional): **Deprecated** - Use options.conflict_mode="overwrite"
- `skip_existing` (bool, optional): **Deprecated** - Use options.conflict_mode="skip"

**Returns:**
- `ImportResult`: Object with fields:
  - `created`: Number of new files created
  - `updated`: Number of files updated
  - `skipped`: Number of files skipped
  - `errors`: Number of errors
  - `collisions`: List of CollisionDetail objects (if any)

**Raises:**
- `ValueError`: If JSONL format is invalid
- `FileNotFoundError`: If input file doesn't exist

**Examples:**

```python
# Import metadata (skip existing by default)
result = nx.import_metadata("backup.jsonl")
print(f"Created: {result.created}, Skipped: {result.skipped}")

# Import with conflict resolution
from nexus.core.export_import import ImportOptions

# Dry-run to preview changes
options = ImportOptions(conflict_mode="auto", dry_run=True)
result = nx.import_metadata("backup.jsonl", options=options)
print(f"Would create {result.created} files")

# Import and overwrite conflicts
options = ImportOptions(conflict_mode="overwrite")
result = nx.import_metadata("backup.jsonl", options=options)

# Import with error on conflicts
options = ImportOptions(conflict_mode="error")
try:
    result = nx.import_metadata("backup.jsonl", options=options)
except ValueError as e:
    print(f"Conflict detected: {e}")
```

---

### batch_get_content_ids()

Get CAS content IDs for multiple file paths in a single operation (efficient batch query).

```python
def batch_get_content_ids(
    paths: list[str]
) -> dict[str, str | None]
```

**Parameters:**
- `paths` (list[str]): List of virtual file paths

**Returns:**
- `dict[str, str | None]`: Mapping of path → content_id (SHA-256 hash), None if file doesn't exist

**Examples:**

```python
# Get content IDs for multiple files
paths = ["/file1.txt", "/file2.txt", "/file3.txt"]
content_ids = nx.batch_get_content_ids(paths)

for path, content_id in content_ids.items():
    if content_id:
        print(f"{path}: {content_id[:16]}...")
    else:
        print(f"{path}: NOT FOUND")

# Find duplicate content
all_paths = nx.list()
content_map = nx.batch_get_content_ids(all_paths)

# Group by content ID
from collections import defaultdict
duplicates = defaultdict(list)
for path, cid in content_map.items():
    if cid:
        duplicates[cid].append(path)

# Show duplicates
for cid, paths in duplicates.items():
    if len(paths) > 1:
        print(f"Duplicate content ({cid[:16]}...):")
        for p in paths:
            print(f"  - {p}")
```

---
## Metadata Store

### Direct Access (Advanced)

For advanced use cases, you can access the metadata store directly:

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Open the metadata database
store = SQLAlchemyMetadataStore("./nexus-data/metadata.db")

# Get file metadata
metadata = store.get("/documents/report.pdf")
print(f"Size: {metadata.size} bytes")
print(f"ETag: {metadata.etag}")
print(f"Created: {metadata.created_at}")

# Add custom metadata
store.set_file_metadata("/documents/report.pdf", "author", "John Doe")
store.set_file_metadata("/documents/report.pdf", "tags", ["quarterly", "financial"])
store.set_file_metadata("/documents/report.pdf", "version", 3)

# Retrieve custom metadata
author = store.get_file_metadata("/documents/report.pdf", "author")
tags = store.get_file_metadata("/documents/report.pdf", "tags")

store.close()
```

### FileMetadata Object

```python
@dataclass
class FileMetadata:
    path: str                      # Virtual path
    backend_name: str              # Backend identifier
    physical_path: str             # Physical storage path
    size: int                      # File size in bytes
    etag: str | None               # ETag (MD5 hash)
    mime_type: str | None          # MIME type
    created_at: datetime | None    # Creation timestamp
    modified_at: datetime | None   # Last modification timestamp
    version: int                   # Version number (always 1 in v0.1.0)
```

---

## See Also

- [File Operations](file-operations.md) - File metadata
- [Configuration](configuration.md) - Metadata storage options
- [Advanced Usage](advanced-usage.md) - Advanced patterns

## Next Steps

1. Use export_metadata() for backups
2. Use import_metadata() for migration
3. Use batch_get_content_ids() for deduplication
