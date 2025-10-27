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

Database Support:
- SQLite: Uses json_extract(), julianday(), datetime()
- PostgreSQL: Uses ::jsonb operators, NOW(), EXTRACT()
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause


def _json_extract(field: str, db_type: str = "sqlite") -> str:
    """Generate database-specific JSON extraction expression.

    Args:
        field: The field containing JSON (e.g., 'fm.value')
        db_type: Database type ('sqlite' or 'postgresql')

    Returns:
        SQL expression to extract the root JSON value as text
    """
    if db_type == "postgresql":
        # PostgreSQL: Cast to jsonb and extract root value as text
        return f"{field}::jsonb#>>'{{}}'"
    else:
        # SQLite: Use json_extract with '$' path
        return f"json_extract({field}, '$')"


def _now(db_type: str = "sqlite") -> str:
    """Generate database-specific current timestamp expression."""
    if db_type == "postgresql":
        return "NOW()"
    else:
        return "datetime('now')"


def _interval_ago(interval: str, db_type: str = "sqlite") -> str:
    """Generate database-specific timestamp interval expression.

    Args:
        interval: Interval string (e.g., '1 hour', '7 days')
        db_type: Database type

    Returns:
        SQL expression for (NOW - interval)
    """
    if db_type == "postgresql":
        return f"NOW() - INTERVAL '{interval}'"
    else:
        # SQLite uses datetime('now', '-1 hour') syntax
        parts = interval.split()
        if len(parts) == 2:
            count, unit = parts
            return f"datetime('now', '-{count} {unit}')"
        return "datetime('now')"


def get_ready_work_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: ready_work_items - files ready for processing (status='ready', no blockers)."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )
    # Extract priority subquery to avoid escape sequences in f-string (Python 3.11 compatibility)
    priority_subquery = "(SELECT fm_priority.value FROM file_metadata fm_priority WHERE fm_priority.path_id = fp.path_id AND fm_priority.key = 'priority')"

    return text(f"""
{create_stmt} ready_work_items AS
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
      AND {je("fm.value")} = 'ready'
  )
  -- Must have no blocking dependencies
  AND NOT EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    JOIN file_metadata fm_blocker ON {je("fm_dep.value")} = fm_blocker.path_id
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'depends_on'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_blocker_status
        WHERE fm_blocker_status.path_id = {je("fm_dep.value")}
          AND fm_blocker_status.key = 'status'
          AND {je("fm_blocker_status.value")} IN ('pending', 'in_progress', 'blocked')
      )
  )
ORDER BY
    CAST({je(priority_subquery)} AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")


def get_pending_work_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: pending_work_items - files with status='pending' ordered by priority."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )
    priority_subquery = "(SELECT fm_priority.value FROM file_metadata fm_priority WHERE fm_priority.path_id = fp.path_id AND fm_priority.key = 'priority')"

    return text(f"""
{create_stmt} pending_work_items AS
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
      AND {je("fm.value")} = 'pending'
  )
ORDER BY
    CAST({je(priority_subquery)} AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")


def get_blocked_work_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: blocked_work_items - files that are blocked by dependencies."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )
    priority_subquery = "(SELECT fm_priority.value FROM file_metadata fm_priority WHERE fm_priority.path_id = fp.path_id AND fm_priority.key = 'priority')"

    return text(f"""
{create_stmt} blocked_work_items AS
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
      AND {je("fm.value")} IN ('blocked', 'ready', 'pending')
  )
  -- Has at least one unresolved blocker
  AND EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'depends_on'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_blocker_status
        WHERE fm_blocker_status.path_id = {je("fm_dep.value")}
          AND fm_blocker_status.key = 'status'
          AND {je("fm_blocker_status.value")} IN ('pending', 'in_progress', 'blocked')
      )
  )
ORDER BY
    blocker_count DESC,
    CAST({je(priority_subquery)} AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")


def get_work_by_priority_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: work_by_priority - all work items ordered by priority and age."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )
    priority_subquery = "(SELECT fm_priority.value FROM file_metadata fm_priority WHERE fm_priority.path_id = fp.path_id AND fm_priority.key = 'priority')"

    return text(f"""
{create_stmt} work_by_priority AS
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
    CAST({je(priority_subquery)} AS INTEGER) ASC NULLS LAST,
    fp.created_at ASC;
""")


def get_in_progress_work_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: in_progress_work - files currently being processed."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    # Avoid escape sequence in f-string (not supported in Python 3.11)
    started_at_subquery = "(SELECT fm.value FROM file_metadata fm WHERE fm.path_id = fp.path_id AND fm.key = 'started_at')"
    order_expr = je(started_at_subquery)
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )

    return text(f"""
{create_stmt} in_progress_work AS
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
      AND {je("fm.value")} = 'in_progress'
  )
ORDER BY
    {order_expr} DESC;
""")


def get_ready_for_indexing_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: ready_for_indexing - files queued for semantic indexing with no pending dependencies."""
    je = lambda f: _json_extract(f, db_type)  # noqa: E731
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )

    return text(f"""
{create_stmt} ready_for_indexing AS
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
      AND {je("fm.value")} = 'queued'
  )
  -- Must have no pending dependencies
  AND NOT EXISTS (
    SELECT 1 FROM file_metadata fm_dep
    WHERE fm_dep.path_id = fp.path_id
      AND fm_dep.key = 'dependencies'
      AND EXISTS (
        SELECT 1 FROM file_metadata fm_dep_status
        WHERE fm_dep_status.path_id = {je("fm_dep.value")}
          AND fm_dep_status.key = 'processing_status'
          AND {je("fm_dep_status.value")} IN ('queued', 'extracting')
      )
  )
ORDER BY fp.size_bytes ASC, fp.created_at ASC;
""")


def get_hot_tier_eviction_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: hot_tier_eviction_candidates - files accessed long ago (cache eviction candidates)."""
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )

    if db_type == "postgresql":
        hours_since_access = "CAST(EXTRACT(EPOCH FROM (NOW() - fp.accessed_at)) / 3600 AS INTEGER)"
        time_threshold = _interval_ago("1 hour", db_type)
    else:
        hours_since_access = "CAST((julianday('now') - julianday(fp.accessed_at)) * 24 AS INTEGER)"
        time_threshold = "datetime('now', '-1 hour')"

    return text(f"""
{create_stmt} hot_tier_eviction_candidates AS
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
    -- Hours since last access
    {hours_since_access} as hours_since_access
FROM file_paths fp
WHERE fp.deleted_at IS NULL
  AND fp.backend_id = 'workspace'  -- Hot tier files
  AND fp.accessed_at IS NOT NULL
  AND fp.accessed_at < {time_threshold}  -- Not accessed in last hour
  AND fp.locked_by IS NULL  -- Not currently locked
ORDER BY fp.accessed_at ASC
LIMIT 1000;
""")


def get_orphaned_content_view(db_type: str = "sqlite") -> TextClause:
    """SQL View: orphaned_content_objects - content chunks with no references (GC targets)."""
    create_stmt = (
        "CREATE OR REPLACE VIEW" if db_type == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    )

    if db_type == "postgresql":
        days_since_access = (
            "CAST(EXTRACT(EPOCH FROM (NOW() - cc.last_accessed_at)) / 86400 AS INTEGER)"
        )
        now_expr = _now(db_type)
        seven_days_ago = _interval_ago("7 days", db_type)
    else:
        days_since_access = "CAST(julianday('now') - julianday(cc.last_accessed_at) AS INTEGER)"
        now_expr = "datetime('now')"
        seven_days_ago = "datetime('now', '-7 days')"

    return text(f"""
{create_stmt} orphaned_content_objects AS
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
    {days_since_access} as days_since_access
FROM content_chunks cc
WHERE cc.ref_count = 0
  AND (cc.protected_until IS NULL OR cc.protected_until < {now_expr})
  AND (cc.last_accessed_at IS NULL OR cc.last_accessed_at < {seven_days_ago})
ORDER BY cc.last_accessed_at ASC NULLS FIRST;
""")


# List of view names and their generator functions
VIEW_GENERATORS = [
    ("ready_work_items", get_ready_work_view),
    ("pending_work_items", get_pending_work_view),
    ("blocked_work_items", get_blocked_work_view),
    ("work_by_priority", get_work_by_priority_view),
    ("in_progress_work", get_in_progress_work_view),
    ("ready_for_indexing", get_ready_for_indexing_view),
    ("hot_tier_eviction_candidates", get_hot_tier_eviction_view),
    ("orphaned_content_objects", get_orphaned_content_view),
]

# For backward compatibility with static views (SQLite only)
ALL_VIEWS = [(name, func("sqlite")) for name, func in VIEW_GENERATORS]

# SQL to drop all views
VIEW_NAMES = [name for name, _ in VIEW_GENERATORS]
DROP_VIEWS = [text(f"DROP VIEW IF EXISTS {name};") for name in VIEW_NAMES]

# Allowlist of valid view names for SQL injection prevention
ALLOWED_VIEW_NAMES = frozenset(name for name, _ in VIEW_GENERATORS)


def get_all_views(db_type: str = "sqlite") -> list[tuple[str, TextClause]]:
    """Get all view definitions for a specific database type.

    Args:
        db_type: Database type ('sqlite' or 'postgresql')

    Returns:
        List of (view_name, view_sql) tuples
    """
    return [(name, func(db_type)) for name, func in VIEW_GENERATORS]


def create_views(engine: Any, db_type: str = "sqlite") -> None:  # noqa: ANN401
    """Create all SQL views for work detection.

    Args:
        engine: SQLAlchemy engine instance
        db_type: Database type ('sqlite' or 'postgresql')
    """
    views = get_all_views(db_type)
    with engine.connect() as conn:
        for _name, view_sql in views:
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
