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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Import Permission and OperationContext from the original module (don't duplicate)
from nexus.core.permissions import OperationContext, PermissionEnforcer

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

    # Tenant management
    MANAGE_TENANTS = "admin:tenants:*"  # Manage tenant isolation

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
    tenant_id: str | None
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
            "tenant_id": self.tenant_id,
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
        self._conn: Any = None
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
                                    tenant_id TEXT,
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
                                "CREATE INDEX idx_audit_tenant_timestamp ON admin_bypass_audit(tenant_id, timestamp)"
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
                                    tenant_id VARCHAR(255),
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
                                "CREATE INDEX idx_audit_tenant_timestamp ON admin_bypass_audit(tenant_id, timestamp)"
                            )
                        )
                        conn.commit()
        except Exception:
            # If table creation fails, it might already exist or migrations handle it
            pass

    def _get_connection(self) -> Any:
        """Get database connection."""
        if self._conn is None:
            self._conn = self.engine.raw_connection()
        return self._conn

    def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s if needed."""
        dialect_name = self.engine.dialect.name
        if dialect_name == "postgresql":
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
        conn = self._get_connection()
        cursor = self._create_cursor(conn)

        cursor.execute(
            self._fix_sql_placeholders(
                """
                INSERT INTO admin_bypass_audit (
                    id, timestamp, request_id, user_id, tenant_id, path,
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
                entry.tenant_id,
                entry.path,
                entry.permission,
                entry.bypass_type,
                entry.allowed,  # Use boolean directly, not int()
                json.dumps(entry.capabilities),
                entry.denial_reason,
            ),
        )

        conn.commit()

    def query_bypasses(
        self,
        user: str | None = None,
        tenant_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log for bypass events.

        Args:
            user: Filter by user ID
            tenant_id: Filter by tenant ID
            start_time: Filter by start timestamp
            end_time: Filter by end timestamp
            limit: Max results to return

        Returns:
            List of audit log entries as dictionaries
        """
        conn = self._get_connection()
        cursor = self._create_cursor(conn)

        where_clauses = []
        params = []

        if user:
            where_clauses.append("user_id = ?")
            params.append(user)

        if tenant_id:
            where_clauses.append("tenant_id = ?")
            params.append(tenant_id)

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
                SELECT id, timestamp, request_id, user_id, tenant_id, path,
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
                    "tenant_id": row["tenant_id"],
                    "path": row["path"],
                    "permission": row["permission"],
                    "bypass_type": row["bypass_type"],
                    "allowed": bool(row["allowed"]),
                    "capabilities": json.loads(row["capabilities"]) if row["capabilities"] else [],
                    "denial_reason": row["denial_reason"],
                }
            )

        return results


# ============================================================================
# Enhanced Operation Context with Admin Capabilities (P0-4)
# ============================================================================
# DEPRECATED: EnhancedOperationContext is now an alias for OperationContext.
# OperationContext now includes all features (admin_capabilities, request_id).
# Use OperationContext directly instead of EnhancedOperationContext.
# ============================================================================


# EnhancedOperationContext is now just an alias for OperationContext
# This maintains backward compatibility while we migrate code to use OperationContext
EnhancedOperationContext = OperationContext


# ============================================================================
# Enhanced Permission Enforcer with P0-4 Fix
# ============================================================================
# DEPRECATED: EnhancedPermissionEnforcer is now an alias for PermissionEnforcer.
# PermissionEnforcer now includes all features (scoped bypass, audit logging, etc.).
# Use PermissionEnforcer directly instead of EnhancedPermissionEnforcer.
# ============================================================================

# EnhancedPermissionEnforcer is now just an alias for PermissionEnforcer
# This maintains backward compatibility while we migrate code to use PermissionEnforcer
EnhancedPermissionEnforcer = PermissionEnforcer
