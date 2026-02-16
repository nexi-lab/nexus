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
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table, insert, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from nexus.storage.models._base import Base
from nexus.storage.models.permissions import AdminBypassAuditModel as ABA

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
    Uses SQLAlchemy ORM via AdminBypassAuditModel.
    """

    def __init__(self, engine: Any):
        """Initialize audit store.

        Args:
            engine: SQLAlchemy database engine
        """
        self.engine = engine
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure audit tables exist using ORM model metadata."""
        with suppress(OperationalError, ProgrammingError):
            Base.metadata.create_all(self.engine, tables=[cast(Table, ABA.__table__)])

    def close(self) -> None:
        """Close database connection (no-op, connections are managed per-operation)."""

    def log_bypass(self, entry: AuditLogEntry) -> None:
        """Log admin/system bypass to immutable audit table.

        Args:
            entry: Audit log entry to record
        """
        with self.engine.begin() as conn:
            conn.execute(
                insert(ABA).values(
                    id=str(uuid.uuid4()),
                    timestamp=entry.timestamp,
                    request_id=entry.request_id,
                    user_id=entry.user,
                    zone_id=entry.zone_id,
                    path=entry.path,
                    permission=entry.permission,
                    bypass_type=entry.bypass_type,
                    allowed=entry.allowed,
                    capabilities=json.dumps(entry.capabilities),
                    denial_reason=entry.denial_reason,
                )
            )

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
        stmt = select(ABA).order_by(ABA.timestamp.desc()).limit(limit)

        if user:
            stmt = stmt.where(ABA.user_id == user)
        if zone_id:
            stmt = stmt.where(ABA.zone_id == zone_id)
        if start_time:
            stmt = stmt.where(ABA.timestamp >= start_time.isoformat())
        if end_time:
            stmt = stmt.where(ABA.timestamp <= end_time.isoformat())

        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()

        results = []
        for row in rows:
            results.append(
                {
                    "id": row.id,
                    "timestamp": row.timestamp,
                    "request_id": row.request_id,
                    "user_id": row.user_id,
                    "zone_id": row.zone_id,
                    "path": row.path,
                    "permission": row.permission,
                    "bypass_type": row.bypass_type,
                    "allowed": bool(row.allowed),
                    "capabilities": json.loads(row.capabilities) if row.capabilities else [],
                    "denial_reason": row.denial_reason,
                }
            )

        return results


# ============================================================================
# DEPRECATED aliases — Issue #1460
# All known import sites have been migrated. These remain only for any
# out-of-tree code that may still reference the old names.
# ============================================================================


def __getattr__(name: str) -> type:  # noqa: N807
    import warnings

    from nexus.core.types import OperationContext
    from nexus.services.permissions.enforcer import PermissionEnforcer

    _ALIASES = {
        "EnhancedOperationContext": OperationContext,
        "EnhancedPermissionEnforcer": PermissionEnforcer,
    }
    if name in _ALIASES:
        warnings.warn(
            f"{name} is deprecated, use {_ALIASES[name].__name__} directly (Issue #1460)",
            DeprecationWarning,
            stacklevel=2,
        )
        return _ALIASES[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
