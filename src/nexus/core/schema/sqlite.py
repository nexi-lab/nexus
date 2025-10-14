"""SQLite schema for Nexus embedded mode.

This is a simplified version of the full schema for embedded mode (v0.1.0).
The full production schema (with multi-tenancy, distributed locking, etc.)
is in postgres.py and will be used in monolithic/distributed modes.
"""

# Embedded mode schema (simplified from full production schema)
SQLITE_SCHEMA = """
-- File Paths (simplified for embedded mode)
-- Based on the full schema in NEXUS_COMPREHENSIVE_ARCHITECTURE.md
-- Omits: tenant_id, agent_id, lock_version, container_tags, etc. (added in v0.2+)
CREATE TABLE IF NOT EXISTS file_paths (
    path TEXT PRIMARY KEY,
    backend_name TEXT NOT NULL,
    physical_path TEXT NOT NULL,

    -- File properties
    size INTEGER NOT NULL,
    etag TEXT,
    mime_type TEXT,
    version INTEGER NOT NULL DEFAULT 1,

    -- Timestamps
    created_at TEXT,
    modified_at TEXT,

    -- Extensibility (for future features)
    metadata TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_backend_name ON file_paths(backend_name);
CREATE INDEX IF NOT EXISTS idx_path_prefix ON file_paths(path);

-- Note: In production (Postgres), additional columns will be added:
--   - tenant_id, agent_id (multi-tenancy)
--   - parent_path, name, is_directory (hierarchy)
--   - content_id (CAS deduplication)
--   - nexus_etag, storage_etag (dual-etag model)
--   - lock_version, locked_by (distributed locking)
--   - container_tags, document_type (processing)
--   - deleted_at (soft delete)
"""


def get_schema_version() -> str:
    """Return the schema version."""
    return "0.1.0-embedded"
