"""PostgreSQL schema for Nexus monolithic/distributed modes.

This is the full production schema based on NEXUS_COMPREHENSIVE_ARCHITECTURE.md.
It will be used in v0.2.0+ for monolithic and distributed deployments.
"""

# Full production schema (to be implemented in v0.2.0+)
POSTGRES_SCHEMA = """
-- This is a placeholder for the full PostgreSQL schema
-- See NEXUS_COMPREHENSIVE_ARCHITECTURE.md lines 1368-1430 for the complete schema

-- Key differences from SQLite embedded mode:
-- 1. Multi-tenancy: tenant_id, agent_id columns
-- 2. Hierarchy: parent_path, name, is_directory
-- 3. CAS: content_id for deduplication
-- 4. Dual-ETag: nexus_etag (stable), storage_etag (backend)
-- 5. Distributed locking: lock_version (fencing), locked_by, locked_at
-- 6. Processing: container_tags, document_type, processing_status, chunk_count
-- 7. Soft deletes: deleted_at column
-- 8. Full-text search: GIN indexes on JSONB
-- 9. RLS policies: Tenant isolation at database level

-- Full schema will include:
-- - tenants table
-- - agents table
-- - api_keys table
-- - file_paths table (complete version)
-- - content_objects table (CAS)
-- - file_versions table (history)
-- - mounts table (backend configurations)
-- - acl_entries table (permissions)
-- - upload_sessions table (multipart uploads)
-- - file_lineage table (recomputation tracking)
-- - jobs table (async processing)
-- - audit_log table (compliance)
-- - agent_groups table (group permissions)

CREATE TABLE IF NOT EXISTS file_paths (
    path_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,  -- REFERENCES tenants(tenant_id)
    agent_id UUID,            -- REFERENCES agents(agent_id)

    path TEXT NOT NULL,
    parent_path TEXT,
    name TEXT NOT NULL,

    content_id VARCHAR(64),   -- Blake2 hash (CAS)

    is_directory BOOLEAN NOT NULL DEFAULT FALSE,
    size BIGINT NOT NULL DEFAULT 0,
    mime_type VARCHAR(255),

    -- DUAL-ETAG MODEL
    nexus_etag VARCHAR(64) NOT NULL,      -- Nexus-stable SHA-256
    storage_etag VARCHAR(255),            -- Backend's native ETag
    etag_computed_at TIMESTAMP,

    version INTEGER NOT NULL DEFAULT 1,

    backend_name TEXT NOT NULL,           -- Changed from backend_type
    physical_path TEXT NOT NULL,          -- Changed from backend_path
    instance_id VARCHAR(100),

    -- FENCING TOKENS (for distributed locking)
    lock_version BIGINT NOT NULL DEFAULT 0,
    locked_by VARCHAR(100),
    locked_at TIMESTAMP,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    accessed_at TIMESTAMP,
    deleted_at TIMESTAMP,

    -- Processing metadata
    container_tags TEXT[] DEFAULT '{}',
    document_type VARCHAR(50),
    processing_status VARCHAR(50),
    processing_job_id UUID,
    chunk_count INTEGER,
    indexed_at TIMESTAMP,

    metadata JSONB
);

-- Indexes (subset)
CREATE INDEX IF NOT EXISTS idx_file_paths_tenant ON file_paths(tenant_id);
CREATE INDEX IF NOT EXISTS idx_file_paths_path ON file_paths(path);
CREATE INDEX IF NOT EXISTS idx_file_paths_backend ON file_paths(backend_name, physical_path);
CREATE INDEX IF NOT EXISTS idx_file_paths_nexus_etag ON file_paths(nexus_etag);

-- Partial unique index for soft delete
CREATE UNIQUE INDEX IF NOT EXISTS idx_file_paths_tenant_path_live
    ON file_paths(tenant_id, path)
    WHERE deleted_at IS NULL;

-- TODO: Add remaining tables and indexes in v0.2.0
"""


def get_schema_version() -> str:
    """Return the schema version."""
    return "0.2.0-postgres-preview"
