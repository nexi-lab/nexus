"""
Enhanced Permission Enforcer with P0-4 Fix

Implements:
- Scoped admin capabilities (instead of blanket bypass)
- Immutable audit logging for all bypass usage
- Kill-switch to disable admin bypass
- Limited system bypass scope

This ensures admins have traceable, scoped access instead of unlimited bypass.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Import Permission and OperationContext from the original module (don't duplicate)
from sqlalchemy.exc import OperationalError, ProgrammingError

if TYPE_CHECKING:
    pass


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
    READ_SYSTEM = "admin:read:/system/*"  # Read /system paths only

    # Write capabilities
    WRITE_SYSTEM = "admin:write:/system/*"  # Write to /system
    WRITE_ALL = "admin:write:*"  # Write any file (dangerous)

    # Delete capabilities
    DELETE_ANY = "admin:delete:*"  # Delete any file (dangerous)
    DELETE_SYSTEM = "admin:delete:/system/*"  # Delete /system paths only

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
        if path.startswith("/system"):
            return f"admin:{permission}:/system/*"

        # Default: require wildcard permission
        return f"admin:{permission}:*"


@dataclass
class AuditLogEntry:
    """Audit log entry for admin/system bypass (P0-4).

    Stored in immutable audit table for security review.
    """

    timestamp: str
    request_id: str
    user: str
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
            "user": self.user,
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

    def __init__(self, engine: Any):
        """Initialize audit store.

        Args:
            engine: SQLAlchemy database engine
        """
        self.engine = engine
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure audit tables exist."""
        # Create table if it doesn't exist (for tests and non-migration scenarios)
        from sqlalchemy import text

        try:
            with self.engine.connect() as conn:
                # Check if table exists
                if self.engine.dialect.name == "sqlite":
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
                elif self.engine.dialect.name == "postgresql":
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
            except Exception:
                sa_conn.rollback()
                raise

    def close(self) -> None:
        """Close database connection (no-op, connections are managed per-operation)."""
        pass

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s if needed."""
        dialect_name = self.engine.dialect.name
        if dialect_name == "postgresql":
            return sql.replace("?", "%s")
        return sql

    def _create_cursor(self, conn: Any) -> Any:
        """Create a database cursor (driver-agnostic).

        Returns a plain DBAPI cursor. Callers that need dict-like row
        access should use ``_rows_as_dicts`` on the fetch results.

        Args:
            conn: DB-API connection object

        Returns:
            Database cursor
        """
        return conn.cursor()

    @staticmethod
    def _rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
        """Convert cursor results to list of dicts using cursor.description.

        Works with any DBAPI-compliant cursor regardless of driver.
        """
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

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
                    entry.user,
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
            for row in self._rows_as_dicts(cursor):
                row["allowed"] = bool(row["allowed"])
                row["capabilities"] = json.loads(row["capabilities"]) if row["capabilities"] else []
                results.append(row)

            return results
