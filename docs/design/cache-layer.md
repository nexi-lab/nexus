# PostgreSQL Content Cache Layer Design

## Overview

A PostgreSQL-based content caching layer for Nexus connectors (GCS, X/Twitter, Gmail, Google Drive, etc.). Enables fast grep, glob, and semantic search without real-time connector access.

**Scope:** Connectors only. Local backend unchanged.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Scope** | Connectors only | Local backend already fast, no caching needed |
| **Sync mode** | On-demand | No auto-sync; call `connector.sync()` explicitly |
| **Indexing** | During sync | Embeddings generated when sync() called, not auto on file change |
| **Version checking** | Writes only | Optimistic locking for write operations |
| **Multi-tenancy** | Filtering | `tenant_id` column with WHERE clause (same as existing tables) |

## What Changes

| Component | Change | Description |
|-----------|--------|-------------|
| **Migration** | New table | `content_cache` table for cached content |
| **BaseConnector.sync()** | New method | Fetches content, writes to cache, generates embeddings |
| **BaseConnector.read()** | Minor update | Check cache first, fall back to backend |
| **BaseConnector.write()** | Minor update | Version check before write, update cache after |

## What Does NOT Change

| Component | Why |
|-----------|-----|
| **grep** | Already calls `read()` which will use cache |
| **glob** | Already queries `file_paths` table (populated by sync) |
| **semantic_search** | Already queries `document_chunks` (populated by sync) |
| **Local backend** | Not using cache layer |

## Architecture

```
connector.sync()                    # On-demand, populates everything
       |
       +---> file_paths             # File metadata (existing table)
       +---> content_cache          # File content (NEW table)
       +---> document_chunks        # Embeddings (existing table)


grep(pattern, path)
       |
       +---> glob(path)             # Get file list from file_paths
       +---> read(file)             # Get content
                |
                +-- Local path?     --> Read from filesystem
                +-- Connector path? --> Read from content_cache
       +---> search in memory       # Unchanged
```

## Database Schema

### New Table: `content_cache`

```sql
CREATE TABLE content_cache (
    cache_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- References
    path_id UUID NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
    tenant_id VARCHAR(255),

    -- Content
    content_text TEXT,              -- Searchable text (parsed or raw)
    content_binary BYTEA,           -- Original binary (optional)
    content_hash VARCHAR(64) NOT NULL,

    -- Size tracking
    original_size_bytes BIGINT NOT NULL,
    cached_size_bytes BIGINT NOT NULL,

    -- Parsing info
    content_type VARCHAR(50) NOT NULL,  -- 'full', 'parsed', 'summary', 'reference'
    parsed_from VARCHAR(50),            -- 'pdf', 'xlsx', 'docx', etc.
    parser_version VARCHAR(20),
    parse_metadata JSONB,

    -- Version control
    backend_version VARCHAR(255),       -- For optimistic locking on writes

    -- Freshness
    synced_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    stale BOOLEAN NOT NULL DEFAULT FALSE,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_content_cache_path UNIQUE (path_id)
);

-- Indexes
CREATE INDEX idx_content_cache_tenant ON content_cache(tenant_id);
CREATE INDEX idx_content_cache_stale ON content_cache(stale) WHERE stale = TRUE;
```

## API Changes

### 1. New: `connector.sync()`

```python
class BaseConnector(Backend):

    def sync(
        self,
        path: str | None = None,      # None = sync all, or specific path
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int = 100 * 1024 * 1024,  # 100MB
        generate_embeddings: bool = True,
        context: OperationContext | None = None,
    ) -> SyncResult:
        """
        Sync content from connector to cache.

        Populates:
        - file_paths (metadata)
        - content_cache (content)
        - document_chunks (embeddings, if generate_embeddings=True)
        """
        for file in self._list_files(path, include_patterns, exclude_patterns):
            # 1. Fetch from backend
            content = self._read_from_backend(file)
            version = self.get_version(file)

            # 2. Parse if needed (PDF, Excel, etc.)
            parsed = self._parse_content(file, content)

            # 3. Write to content_cache
            self._write_to_cache(file, content, parsed, version)

            # 4. Generate embeddings
            if generate_embeddings:
                semantic_search.index_document(file)

        return SyncResult(...)

@dataclass
class SyncResult:
    files_scanned: int
    files_synced: int
    files_skipped: int
    bytes_synced: int
    embeddings_generated: int
    errors: list[str]
```

**Usage:**
```python
# Sync entire connector
gcs_connector.sync()

# Sync specific folder
gcs_connector.sync(path="/reports/2024/")

# Sync single file
gcs_connector.sync(path="/data/report.pdf")

# Sync with filters
gcs_connector.sync(
    include_patterns=["*.py", "*.md"],
    exclude_patterns=["test_*"],
)
```

### 2. Updated: `connector.read()`

```python
def read(self, path: str, context: OperationContext | None = None) -> bytes:
    """Read content - checks cache first."""

    # Check cache
    cached = self._get_from_cache(path)
    if cached and not cached.stale:
        return cached.content_text or cached.content_binary

    # Fall back to backend
    return self._read_from_backend(path, context)
```

### 3. Updated: `connector.write()`

```python
def write(
    self,
    path: str,
    content: bytes,
    expected_version: str | None = None,  # For optimistic locking
    context: OperationContext | None = None,
) -> WriteResult:
    """Write content with optional version check."""

    # 1. Version check (if requested)
    if expected_version:
        current_version = self.get_version(path)
        if current_version != expected_version:
            raise VersionConflictError(path, expected_version, current_version)

    # 2. Write to backend
    self._write_to_backend(path, content, context)
    new_version = self.get_version(path)

    # 3. Update cache
    self._write_to_cache(path, content, version=new_version)

    return WriteResult(path=path, version=new_version)
```

## Content Type Strategy

| File Type | Size | Strategy | Cached Content |
|-----------|------|----------|----------------|
| Text (.py, .md, .txt) | < 10MB | `full` | Full text |
| Text | > 10MB | `summary` | First 100KB |
| PDF | Any | `parsed` | Extracted text |
| Excel/CSV | Any | `parsed` | Extracted text |
| Word/Docs | Any | `parsed` | Extracted text |
| Binary/Unknown | Any | `reference` | Metadata only |

## Parsers

Shared parsers for content extraction:

```python
# src/nexus/cache/parsers/
├── base.py      # ContentParser base class
├── text.py      # Plain text
├── pdf.py       # PDF (pypdf)
├── excel.py     # Excel (openpyxl)
├── csv.py       # CSV
└── docx.py      # Word (python-docx)
```

## Flow Diagrams

### Sync Flow

```
connector.sync(path="/reports/")
        |
        v
+------------------+
| List files       |
| from backend     |
+--------+---------+
         |
         v
+------------------+
| For each file:   |
| 1. Fetch content |
| 2. Parse if PDF/ |
|    Excel/etc     |
| 3. Write to      |
|    content_cache |
| 4. Index for     |
|    semantic srch |
+------------------+
```

### Read Flow

```
connector.read(path)
        |
        v
+------------------+
| Check cache      |
+--------+---------+
         |
    +----+----+
    |         |
    v         v
  HIT       MISS
    |         |
    v         v
 Return    Read from
 cached    backend
 content     |
             v
          Return
          content
```

### Write Flow (with version check)

```
connector.write(path, content, expected_version="v1")
        |
        v
+------------------+
| Get current      |
| backend version  |
+--------+---------+
         |
    +----+----+
    |         |
    v         v
 v1==v1    v1!=v2
 (match)   (mismatch)
    |         |
    v         v
 Write     Raise
 to        VersionConflict
 backend   Error
    |
    v
 Update
 cache
```

## Error Handling

```python
class VersionConflictError(Exception):
    """Write rejected - backend version changed."""
    def __init__(self, path: str, expected: str, actual: str):
        self.path = path
        self.expected_version = expected
        self.actual_version = actual
```

## CLI Commands

```bash
# Sync connector content
nexus sync /mnt/gcs/                     # Sync entire mount
nexus sync /mnt/gcs/reports/             # Sync folder
nexus sync /mnt/gcs/report.pdf           # Sync single file

# Check cache status
nexus cache status /mnt/gcs/report.pdf

# Invalidate cache
nexus cache invalidate /mnt/gcs/

# Cache stats
nexus cache stats
```

## Implementation Plan

1. **Migration** - Add `content_cache` table
2. **Cache utilities** - Simple read/write functions for cache table
3. **Parsers** - PDF, Excel, etc. (can add incrementally)
4. **BaseConnector.sync()** - New method
5. **BaseConnector.read()** - Check cache first
6. **BaseConnector.write()** - Version check + update cache
7. **CLI commands** - `nexus sync`, `nexus cache`

## Summary

This is a minimal cache layer:
- **1 new table** (`content_cache`)
- **1 new method** (`sync()`)
- **2 minor updates** (`read()`, `write()`)
- **No changes** to grep/glob/semantic_search
