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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

# Import Permission from the original module (don't duplicate)
from nexus.core.permissions import Permission

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager


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

    def log_bypass(self, entry: AuditLogEntry) -> None:
        """Log admin/system bypass to immutable audit table.

        Args:
            entry: Audit log entry to record
        """
        conn = self._get_connection()
        cursor = conn.cursor()

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
        cursor = conn.cursor()

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
            if hasattr(row, "keys"):
                results.append(dict(row))
            else:
                results.append(
                    {
                        "id": row[0],
                        "timestamp": row[1],
                        "request_id": row[2],
                        "user_id": row[3],
                        "tenant_id": row[4],
                        "path": row[5],
                        "permission": row[6],
                        "bypass_type": row[7],
                        "allowed": bool(row[8]),
                        "capabilities": json.loads(row[9]) if row[9] else [],
                        "denial_reason": row[10],
                    }
                )

        return results


# ============================================================================
# Enhanced Operation Context with Admin Capabilities (P0-4)
# ============================================================================


@dataclass
class EnhancedOperationContext:
    """Operation context with admin capabilities and subject identity (P0-2, P0-4).

    P0-2: Subject-based identity (user, agent, service, session)
    P0-4: Admin capabilities and audit trail

    Attributes:
        user: Subject ID (LEGACY: use subject_id)
        subject_type: Type of subject (user, agent, service, session)
        subject_id: Unique identifier for the subject
        groups: List of group IDs
        tenant_id: Tenant/organization ID
        agent_id: DEPRECATED - use subject_type + subject_id
        is_admin: Admin privileges flag
        is_system: System operation flag
        admin_capabilities: Set of granted admin capabilities (P0-4)
        request_id: Unique ID for audit trail correlation (P0-4)
    """

    user: str  # LEGACY
    groups: list[str]
    tenant_id: str | None = None
    agent_id: str | None = None  # DEPRECATED
    is_admin: bool = False
    is_system: bool = False
    admin_capabilities: set[str] = field(default_factory=set)  # P0-4
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # P0-4

    # P0-2: Subject-based identity
    subject_type: str = "user"
    subject_id: str | None = None

    def __post_init__(self) -> None:
        """Validate context and apply P0-2 subject defaults."""
        # P0-2: If subject_id not provided, use user field for backward compatibility
        if self.subject_id is None:
            self.subject_id = self.user

        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC (P0-2).

        Returns:
            Tuple of (subject_type, subject_id)
        """
        return (self.subject_type, self.subject_id or self.user)


# ============================================================================
# Enhanced Permission Enforcer with P0-4 Fix
# ============================================================================


class EnhancedPermissionEnforcer:
    """Permission enforcer with scoped admin bypass and audit logging (P0-4).

    Improvements over PermissionEnforcer:
    - Admin bypass requires explicit capabilities
    - All bypasses logged to immutable audit store
    - Kill-switch to disable admin bypass
    - System bypass limited to /system paths
    """

    def __init__(
        self,
        metadata_store: Any = None,
        rebac_manager: EnhancedReBACManager | None = None,
        allow_admin_bypass: bool = False,  # P0-4: Kill-switch DEFAULT OFF for production security
        allow_system_bypass: bool = True,  # P0-4: System bypass still enabled (for service operations)
        audit_store: AuditStore | None = None,  # P0-4: Audit logging
        admin_bypass_paths: list[str] | None = None,  # P0-4: Scoped bypass (allowlist)
    ):
        """Initialize enhanced permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup
            rebac_manager: ReBAC manager for permissions
            allow_admin_bypass: Enable admin bypass (DEFAULT: False for security)
            allow_system_bypass: Enable system bypass (for internal operations)
            audit_store: Audit store for bypass logging
            admin_bypass_paths: Optional path allowlist for admin bypass (e.g., ["/admin/*"])
        """
        self.metadata_store = metadata_store
        self.rebac_manager = rebac_manager
        self.allow_admin_bypass = allow_admin_bypass  # P0-4
        self.allow_system_bypass = allow_system_bypass  # P0-4
        self.audit_store = audit_store  # P0-4
        self.admin_bypass_paths = admin_bypass_paths or []  # P0-4: Scoped bypass

    def check(
        self,
        path: str,
        permission: Permission,
        context: EnhancedOperationContext,
    ) -> bool:
        """Check permission with scoped admin bypass and audit logging (P0-4).

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Enhanced operation context with capabilities

        Returns:
            True if permission is granted, False otherwise
        """
        # Map Permission enum to string
        permission_str = self._permission_to_string(permission)

        # P0-4: System bypass (limited scope)
        if context.is_system:
            if not self.allow_system_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "system", "kill_switch_disabled"
                )
                raise PermissionError("System bypass disabled by configuration")

            if not self._is_allowed_system_operation(path, permission_str):
                self._log_bypass_denied(context, path, permission_str, "system", "scope_limit")
                raise PermissionError(f"System bypass not allowed for {path}")

            self._log_bypass(context, path, permission_str, "system", allowed=True)
            return True

        # P0-4: Admin bypass (capability-based + path-scoped)
        if context.is_admin:
            if not self.allow_admin_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "kill_switch_disabled"
                )
                # Fall through to ReBAC check instead of denying
                return self._check_rebac(path, permission, context)

            # P0-4: Check path-based allowlist (scoped bypass)
            if self.admin_bypass_paths and not self._path_matches_allowlist(
                path, self.admin_bypass_paths
            ):
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "path_not_in_allowlist"
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            required_capability = AdminCapability.get_required_capability(path, permission_str)
            if required_capability not in context.admin_capabilities:
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"missing_capability_{required_capability}",
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            self._log_bypass(context, path, permission_str, "admin", allowed=True)
            return True

        # Normal ReBAC check
        return self._check_rebac(path, permission, context)

    def _is_allowed_system_operation(self, path: str, permission: str) -> bool:
        """Check if system bypass is allowed for this operation (P0-4).

        System bypass is limited to:
        - /system/* paths only
        - Read, write, execute operations

        Args:
            path: File path
            permission: Permission type

        Returns:
            True if system bypass is allowed
        """
        # System bypass only allowed for /system paths
        if not path.startswith("/system"):
            return False

        # Allow common operations
        return permission in ["read", "write", "execute", "delete"]

    def _log_bypass(
        self,
        context: EnhancedOperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        allowed: bool,
    ) -> None:
        """Log admin/system bypass to audit store (P0-4)."""
        if not self.audit_store:
            return

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            tenant_id=context.tenant_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=allowed,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
        )

        self.audit_store.log_bypass(entry)

    def _log_bypass_denied(
        self,
        context: EnhancedOperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        reason: str,
    ) -> None:
        """Log denied bypass attempt (P0-4)."""
        if not self.audit_store:
            return

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            tenant_id=context.tenant_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=False,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
            denial_reason=reason,
        )

        self.audit_store.log_bypass(entry)

    def _check_rebac(
        self,
        path: str,
        permission: Permission,
        context: EnhancedOperationContext,
    ) -> bool:
        """Check ReBAC relationships for permission."""
        if not self.rebac_manager:
            return False

        permission_name = self._permission_to_string(permission)

        # P0-2: Pass tenant_id to EnhancedReBACManager
        # For single-tenant deployments, use "default" as tenant_id
        tenant_id = context.tenant_id if context.tenant_id else "default"

        # Check ReBAC permission
        return self.rebac_manager.rebac_check(
            subject=context.get_subject(),  # P0-2: Use typed subject
            permission=permission_name,
            object=("file", path),
            tenant_id=tenant_id,
        )

    def _permission_to_string(self, permission: Permission) -> str:
        """Convert Permission enum to string."""
        if permission & Permission.READ:
            return "read"
        elif permission & Permission.WRITE:
            return "write"
        elif permission & Permission.EXECUTE:
            return "execute"
        elif permission & Permission.NONE:
            return "none"
        else:
            return "unknown"

    def _path_matches_allowlist(self, path: str, allowlist: list[str]) -> bool:
        """Check if path matches any pattern in allowlist.

        P0-4: Scoped admin bypass - only allow admin bypass for specific paths

        Args:
            path: File path to check
            allowlist: List of path patterns (supports wildcards: /admin/*, /workspace/*)

        Returns:
            True if path matches any allowlist pattern
        """
        import fnmatch

        return any(fnmatch.fnmatch(path, pattern) for pattern in allowlist)

    def filter_list(
        self,
        paths: list[str],
        context: EnhancedOperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission.

        Args:
            paths: List of paths to filter
            context: Operation context

        Returns:
            Filtered list of paths user can read
        """
        # Admin/system bypass
        if (context.is_admin and self.allow_admin_bypass) or (
            context.is_system and self.allow_system_bypass
        ):
            return paths

        # Filter by ReBAC permissions
        result = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                result.append(path)

        return result
