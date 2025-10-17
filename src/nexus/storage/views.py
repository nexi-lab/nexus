"""SQL views for efficient work detection and resource management queries.

This module defines SQL views that enable efficient querying for:

Work Queue Views (Issue #69):
- Ready work items (files with status='ready' and no blockers)
- Pending work (files with status='pending')
- Blocked work (files with unresolved dependencies)
- In-progress work (files currently being processed)
- Work by priority (all work sorted by priority)

Resource Management Views (Issue #36):
- Ready for indexing (files queued for semantic indexing)
- Hot tier eviction candidates (cache eviction based on access time)
- Orphaned content objects (garbage collection targets)

These views are optimized for O(n) performance using indexed queries.
"""

from typing import Any

from sqlalchemy import text

# SQL View: ready_work_items
# Finds files that are ready for processing (status='ready', no blockers)
READY_WORK_VIEW = text("""
CREATE VIEW IF NOT EXISTS ready_work_items AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.physical_path,
    fp.file_type,
    fp.size_bytes,
    fp.content_hash,
    fp.created_at,
    fp.updated_at,
    -- Include status and priority from metadata
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'status') as status,
    (SELECT fm_priority.value
     FROM file_metadata fm_priority
     WHERE fm_priority.path_id = fp.path_id
       AND fm_priority.key = 'priority') as priority
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  -- Must have status='ready'
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'status'
      AND json_extract(fm.value, '$') = 'ready'
  )
  -- Must have no blocking dependencies
  AND NOT EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    JOIN file_metadata fm_blocker ON json_extract(fm_dep.value, '$') = fm_blocker.path_id
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'depends_on'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_blocker_status
        WHERE fm_blocker_status.path_id = json_extract(fm_dep.value, '$')
          AND fm_blocker_status.key = 'status'
          AND json_extract(fm_blocker_status.value, '$') IN ('pending', 'in_progress', 'blocked')
      )
  )
ORDER BY
    CAST(json_extract(priority, '$') AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")

# SQL View: pending_work_items
# Finds files with status='pending' ordered by priority
PENDING_WORK_VIEW = text("""
CREATE VIEW IF NOT EXISTS pending_work_items AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.physical_path,
    fp.file_type,
    fp.size_bytes,
    fp.content_hash,
    fp.created_at,
    fp.updated_at,
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'status') as status,
    (SELECT fm_priority.value
     FROM file_metadata fm_priority
     WHERE fm_priority.path_id = fp.path_id
       AND fm_priority.key = 'priority') as priority
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'status'
      AND json_extract(fm.value, '$') = 'pending'
  )
ORDER BY
    CAST(json_extract(priority, '$') AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")

# SQL View: blocked_work_items
# Finds files that are blocked by dependencies
BLOCKED_WORK_VIEW = text("""
CREATE VIEW IF NOT EXISTS blocked_work_items AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.physical_path,
    fp.file_type,
    fp.size_bytes,
    fp.content_hash,
    fp.created_at,
    fp.updated_at,
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'status') as status,
    (SELECT fm_priority.value
     FROM file_metadata fm_priority
     WHERE fm_priority.path_id = fp.path_id
       AND fm_priority.key = 'priority') as priority,
    -- Count of blocking dependencies
    (SELECT COUNT(*)
     FROM file_metadata fm_dep
     WHERE fm_dep.path_id = fp.path_id
       AND fm_dep.key = 'depends_on') as blocker_count
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'status'
      AND json_extract(fm.value, '$') IN ('blocked', 'ready', 'pending')
  )
  -- Has at least one unresolved blocker
  AND EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'depends_on'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_blocker_status
        WHERE fm_blocker_status.path_id = json_extract(fm_dep.value, '$')
          AND fm_blocker_status.key = 'status'
          AND json_extract(fm_blocker_status.value, '$') IN ('pending', 'in_progress', 'blocked')
      )
  )
ORDER BY
    blocker_count DESC,
    CAST(json_extract(priority, '$') AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")

# SQL View: work_by_priority
# All work items ordered by priority and age
WORK_BY_PRIORITY_VIEW = text("""
CREATE VIEW IF NOT EXISTS work_by_priority AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.file_type,
    fp.size_bytes,
    fp.created_at,
    fp.updated_at,
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'status') as status,
    (SELECT fm_priority.value
     FROM file_metadata fm_priority
     WHERE fm_priority.path_id = fp.path_id
       AND fm_priority.key = 'priority') as priority,
    (SELECT fm_tags.value
     FROM file_metadata fm_tags
     WHERE fm_tags.path_id = fp.path_id
       AND fm_tags.key = 'tags') as tags
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'status'
  )
ORDER BY
    CAST(json_extract(priority, '$') AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")

# SQL View: in_progress_work
# Files currently being processed
IN_PROGRESS_WORK_VIEW = text("""
CREATE VIEW IF NOT EXISTS in_progress_work AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.file_type,
    fp.size_bytes,
    fp.created_at,
    fp.updated_at,
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'status') as status,
    (SELECT fm_worker.value
     FROM file_metadata fm_worker
     WHERE fm_worker.path_id = fp.path_id
       AND fm_worker.key = 'worker_id') as worker_id,
    (SELECT fm_started.value
     FROM file_metadata fm_started
     WHERE fm_started.path_id = fp.path_id
       AND fm_started.key = 'started_at') as started_at
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'status'
      AND json_extract(fm.value, '$') = 'in_progress'
  )
ORDER BY
    json_extract((SELECT fm.value FROM file_metadata fm
                  WHERE fm.path_id = fp.path_id AND fm.key = 'started_at'), '$') DESC;
""")

# SQL View: ready_for_indexing (Issue #36)
# Files queued for semantic indexing with no pending dependencies
READY_FOR_INDEXING_VIEW = text("""
CREATE VIEW IF NOT EXISTS ready_for_indexing AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.physical_path,
    fp.file_type,
    fp.size_bytes,
    fp.content_hash,
    fp.created_at,
    fp.updated_at,
    (SELECT fm_status.value
     FROM file_metadata fm_status
     WHERE fm_status.path_id = fp.path_id
       AND fm_status.key = 'processing_status') as processing_status
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  -- Must have processing_status='queued'
  AND EXISTS (
    SELECT 1 FROM file_metadata fm
    WHERE fm.path_id = fp.path_id
      AND fm.key = 'processing_status'
      AND json_extract(fm.value, '$') = 'queued'
  )
  -- Must have no pending dependencies
  AND NOT EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'dependencies'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_dep_status
        WHERE fm_dep_status.path_id = json_extract(fm_dep.value, '$')
          AND fm_dep_status.key = 'processing_status'
          AND json_extract(fm_dep_status.value, '$') IN ('queued', 'extracting')
      )
  )
ORDER BY fp.size_bytes ASC, fp.created_at ASC;
""")

# SQL View: hot_tier_eviction_candidates (Issue #36)
# Files accessed long ago that can be evicted from hot tier (cache)
HOT_TIER_EVICTION_VIEW = text("""
CREATE VIEW IF NOT EXISTS hot_tier_eviction_candidates AS
SELECT
    fp.path_id,
    fp.tenant_id,
    fp.virtual_path,
    fp.backend_id,
    fp.physical_path,
    fp.file_type,
    fp.size_bytes,
    fp.content_hash,
    fp.created_at,
    fp.updated_at,
    fp.accessed_at,
    fp.locked_by,
    -- Hours since last access (SQLite doesn't have EXTRACT, use julianday)
    CAST((julianday('now') - julianday(fp.accessed_at)) * 24 AS INTEGER) as hours_since_access
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND fp.backend_id = 'workspace'  -- Hot tier files
  AND fp.accessed_at IS NOT NULL
  AND fp.accessed_at < datetime('now', '-1 hour')  -- Not accessed in last hour
  AND fp.locked_by IS NULL  -- Not currently locked
ORDER BY fp.accessed_at ASC
LIMIT 1000;
""")

# SQL View: orphaned_content_objects (Issue #36)
# Content chunks with no references that can be garbage collected
ORPHANED_CONTENT_VIEW = text("""
CREATE VIEW IF NOT EXISTS orphaned_content_objects AS
SELECT
    cc.chunk_id,
    cc.content_hash,
    cc.size_bytes,
    cc.storage_path,
    cc.ref_count,
    cc.created_at,
    cc.last_accessed_at,
    cc.protected_until,
    -- Days since last access
    CAST(julianday('now') - julianday(cc.last_accessed_at) AS INTEGER) as days_since_access
FROM content_chunks cc
WHERE cc.ref_count = 0
  AND (cc.protected_until IS NULL OR cc.protected_until < datetime('now'))
  AND (cc.last_accessed_at IS NULL OR cc.last_accessed_at < datetime('now', '-7 days'))
ORDER BY cc.last_accessed_at ASC NULLS FIRST;
""")

# List of all views to create
ALL_VIEWS = [
    ("ready_work_items", READY_WORK_VIEW),
    ("pending_work_items", PENDING_WORK_VIEW),
    ("blocked_work_items", BLOCKED_WORK_VIEW),
    ("work_by_priority", WORK_BY_PRIORITY_VIEW),
    ("in_progress_work", IN_PROGRESS_WORK_VIEW),
    ("ready_for_indexing", READY_FOR_INDEXING_VIEW),
    ("hot_tier_eviction_candidates", HOT_TIER_EVICTION_VIEW),
    ("orphaned_content_objects", ORPHANED_CONTENT_VIEW),
]

# SQL to drop all views
DROP_VIEWS = [text(f"DROP VIEW IF EXISTS {name};") for name, _ in ALL_VIEWS]


def create_views(engine: Any) -> None:  # noqa: ANN401
    """Create all SQL views for work detection.

    Args:
        engine: SQLAlchemy engine instance
    """
    with engine.connect() as conn:
        for _name, view_sql in ALL_VIEWS:
            conn.execute(view_sql)
            conn.commit()


def drop_views(engine: Any) -> None:  # noqa: ANN401
    """Drop all SQL views.

    Args:
        engine: SQLAlchemy engine instance
    """
    with engine.connect() as conn:
        for drop_sql in DROP_VIEWS:
            conn.execute(drop_sql)
            conn.commit()
