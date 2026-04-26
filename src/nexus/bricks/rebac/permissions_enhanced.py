"""
Enhanced Permission Enforcer with P0-4 Fix

Implements:
- Scoped admin capabilities (instead of blanket bypass)
- Immutable audit logging for all bypass usage
- Kill-switch to disable admin bypass
- Limited system bypass scope

This ensures admins have traceable, scoped access instead of unlimited bypass.
"""

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError, ProgrammingError

# ============================================================================
# P0-4: Admin Capabilities and Audit System
# ============================================================================


class AdminCapability:
    """Admin capabilities for scoped bypass (P0-4).

    Instead of blanket admin access, admins must have specific capabilities.
    This prevents privilege escalation and ensures audit trails.
    """

    # Bootstrap capability (one-time initial setup)
    BOOTSTRAP = "admin:bootstrap"

    # Read capabilities
    READ_ALL = "admin:read:*"  # Read any file
    READ_SYSTEM = "admin:read:/__sys__/*"  # Read /__sys__ paths only

    # Write capabilities
    WRITE_SYSTEM = "admin:write:/__sys__/*"  # Write to /__sys__
    WRITE_ALL = "admin:write:*"  # Write any file (dangerous)

    # Delete capabilities
    DELETE_ANY = "admin:delete:*"  # Delete any file (dangerous)
    DELETE_SYSTEM = "admin:delete:/__sys__/*"  # Delete /__sys__ paths only

    # ReBAC management
    MANAGE_REBAC = "admin:rebac:*"  # Manage permissions

    # Zone management
    MANAGE_ZONES = "admin:zones:*"  # Manage zone isolation

    @staticmethod
    def get_required_capability(path: str, permission: str) -> str:
        """Determine required admin capability for operation.

        Args:
            path: File path
            permission: Permission type (read, write, delete)

        Returns:
            Required capability string
        """
        # System paths require specific capabilities
        if path.startswith("/__sys__"):
            return f"admin:{permission}:/__sys__/*"

        # Default: require wildcard permission
        return f"admin:{permission}:*"


@dataclass
class AuditLogEntry:
    """Audit log entry for admin/system bypass (P0-4).

    Stored in immutable audit table for security review.
    """

    timestamp: str
    request_id: str
    user_id: str
    zone_id: str | None
    path: str
    permission: str
    bypass_type: str  # "system" or "admin"
    allowed: bool
    capabilities: list[str]
    denial_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "zone_id": self.zone_id,
            "path": self.path,
            "permission": self.permission,
            "bypass_type": self.bypass_type,
            "allowed": self.allowed,
            "capabilities": json.dumps(self.capabilities),
            "denial_reason": self.denial_reason,
        }


class AuditStore:
    """Immutable audit log store for admin/system bypass tracking (P0-4).

    Provides append-only audit trail for all bypass attempts.
    """

    def __init__(self, engine: Any, *, is_postgresql: bool = False):
        """Initialize audit store.

        Args:
            engine: SQLAlchemy database engine
            is_postgresql: Whether the database is PostgreSQL (config-time flag).
        """
        self.engine = engine
        self._is_postgresql = is_postgresql
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure audit tables exist."""
        # Create table if it doesn't exist (for tests and non-migration scenarios)
        from sqlalchemy import text

        try:
            with self.engine.connect() as conn:
                # Check if table exists
                if not self._is_postgresql:
                    result = conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='admin_bypass_audit'"
                        )
                    )
                    if not result.fetchone():
                        # Create table (SQLite syntax)
                        conn.execute(
                            text("""
                                CREATE TABLE admin_bypass_audit (
                                    id TEXT PRIMARY KEY,
                                    timestamp DATETIME NOT NULL,
                                    request_id TEXT NOT NULL,
                                    user_id TEXT NOT NULL,
                                    zone_id TEXT,
                                    path TEXT NOT NULL,
                                    permission TEXT NOT NULL,
                                    bypass_type TEXT NOT NULL,
                                    allowed INTEGER NOT NULL,
                                    capabilities TEXT,
                                    denial_reason TEXT
                                )
                            """)
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_timestamp ON admin_bypass_audit(timestamp)"
                            )
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_user_timestamp ON admin_bypass_audit(user_id, timestamp)"
                            )
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_zone_timestamp ON admin_bypass_audit(zone_id, timestamp)"
                            )
                        )
                        conn.commit()
                else:
                    result = conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'admin_bypass_audit'"
                        )
                    )
                    if not result.fetchone():
                        # Create table (PostgreSQL syntax)
                        conn.execute(
                            text("""
                                CREATE TABLE admin_bypass_audit (
                                    id VARCHAR(36) PRIMARY KEY,
                                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                                    request_id VARCHAR(36) NOT NULL,
                                    user_id VARCHAR(255) NOT NULL,
                                    zone_id VARCHAR(255),
                                    path TEXT NOT NULL,
                                    permission VARCHAR(50) NOT NULL,
                                    bypass_type VARCHAR(20) NOT NULL,
                                    allowed BOOLEAN NOT NULL,
                                    capabilities TEXT,
                                    denial_reason TEXT
                                )
                            """)
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_timestamp ON admin_bypass_audit(timestamp)"
                            )
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_user_timestamp ON admin_bypass_audit(user_id, timestamp)"
                            )
                        )
                        conn.execute(
                            text(
                                "CREATE INDEX idx_audit_zone_timestamp ON admin_bypass_audit(zone_id, timestamp)"
                            )
                        )
                        conn.commit()
        except (OperationalError, ProgrammingError):
            # If table creation fails, it might already exist or migrations handle it
            pass

    @contextmanager
    def _connection(self) -> Any:
        """Context manager for database connections.

        Uses engine.connect() which properly goes through the connection pool
        and respects pool_pre_ping for automatic stale connection detection.
        """
        with self.engine.connect() as sa_conn:
            dbapi_conn = sa_conn.connection.dbapi_connection
            try:
                yield dbapi_conn
                sa_conn.commit()
            except Exception:  # rollback-then-reraise: ensures transaction cleanup
                sa_conn.rollback()
                raise

    def close(self) -> None:
        """Close database connection (no-op, connections are managed per-operation)."""
        pass

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s if needed."""
        if self._is_postgresql:
            return sql.replace("?", "%s")
        return sql

    def _create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate cursor factory for the database type.

        For PostgreSQL: Uses RealDictCursor to return dict-like rows
        For SQLite: Ensures Row factory is set for dict-like access

        Args:
            conn: DB-API connection object

        Returns:
            Database cursor
        """
        # Detect database type based on underlying DBAPI connection
        # SQLAlchemy wraps connections in _ConnectionFairy, need to check dbapi_connection
        actual_conn = conn.dbapi_connection if hasattr(conn, "dbapi_connection") else conn
        conn_module = type(actual_conn).__module__

        # Check if this is a PostgreSQL connection (psycopg2)
        if "psycopg2" in conn_module:
            try:
                import psycopg2.extras

                return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            except (ImportError, AttributeError):
                return conn.cursor()
        elif "sqlite3" in conn_module:
            # SQLite: Ensure Row factory is set for dict-like access
            import sqlite3

            if not hasattr(actual_conn, "row_factory") or actual_conn.row_factory is None:
                actual_conn.row_factory = sqlite3.Row
            return conn.cursor()
        else:
            # Other database - use default cursor
            return conn.cursor()

    def log_bypass(self, entry: AuditLogEntry) -> None:
        """Log admin/system bypass to immutable audit table.

        Args:
            entry: Audit log entry to record
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO admin_bypass_audit (
                        id, timestamp, request_id, user_id, zone_id, path,
                        permission, bypass_type, allowed, capabilities, denial_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    str(uuid.uuid4()),
                    entry.timestamp,
                    entry.request_id,
                    entry.user_id,
                    entry.zone_id,
                    entry.path,
                    entry.permission,
                    entry.bypass_type,
                    entry.allowed,  # Use boolean directly, not int()
                    json.dumps(entry.capabilities),
                    entry.denial_reason,
                ),
            )
            # commit handled by context manager

    def query_bypasses(
        self,
        user: str | None = None,
        zone_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log for bypass events.

        Args:
            user: Filter by user ID
            zone_id: Filter by zone ID
            start_time: Filter by start timestamp
            end_time: Filter by end timestamp
            limit: Max results to return

        Returns:
            List of audit log entries as dictionaries
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            where_clauses = []
            params: list[Any] = []

            if user:
                where_clauses.append("user_id = ?")
                params.append(user)

            if zone_id:
                where_clauses.append("zone_id = ?")
                params.append(zone_id)

            if start_time:
                where_clauses.append("timestamp >= ?")
                params.append(start_time.isoformat())

            if end_time:
                where_clauses.append("timestamp <= ?")
                params.append(end_time.isoformat())

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            cursor.execute(
                self._fix_sql_placeholders(
                    f"""
                    SELECT id, timestamp, request_id, user_id, zone_id, path,
                           permission, bypass_type, allowed, capabilities, denial_reason
                    FROM admin_bypass_audit
                    WHERE {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """
                ),
                (*params, limit),
            )

            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "id": row["id"],
                        "timestamp": row["timestamp"],
                        "request_id": row["request_id"],
                        "user_id": row["user_id"],
                        "zone_id": row["zone_id"],
                        "path": row["path"],
                        "permission": row["permission"],
                        "bypass_type": row["bypass_type"],
                        "allowed": bool(row["allowed"]),
                        "capabilities": json.loads(row["capabilities"])
                        if row["capabilities"]
                        else [],
                        "denial_reason": row["denial_reason"],
                    }
                )

            return results
