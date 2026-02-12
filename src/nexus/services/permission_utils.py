"""Shared permission checking utilities for Nexus services.

Centralizes permission check logic used by MountCoreService and SyncService
to prevent code duplication and ensure consistent error handling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core.context_utils import get_user_identity, get_zone_id

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


class PermissionCheckError(Exception):
    """Raised when the permission system itself is unavailable.

    Distinct from a permission denial — this indicates infrastructure failure
    (e.g., ReBAC backend unreachable, DB connection timeout).
    """


def check_permission(
    gw: NexusFSGateway,
    path: str,
    permission: str,
    context: OperationContext | None,
) -> bool:
    """Check if user has permission on path.

    Args:
        gw: NexusFS gateway for ReBAC checks
        path: Virtual path to check
        permission: Permission to check ("read", "write", "owner")
        context: Operation context

    Returns:
        True if user has permission, False if denied

    Raises:
        PermissionCheckError: If the permission system is unavailable
            (connection errors, timeouts). Callers should handle this
            separately from a permission denial (False return).
    """
    if not context:
        # No context = allow (backward compatibility)
        return True

    # Admin users bypass permission checks
    is_admin = getattr(context, "is_admin", False)
    if is_admin:
        return True

    subject_type, subject_id = get_user_identity(context)
    if not subject_id:
        return False

    zone_id = get_zone_id(context)

    try:
        return gw.rebac_check(
            subject=(subject_type, subject_id),
            permission=permission,
            object=("file", path),
            zone_id=zone_id,
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        # Infrastructure failures should propagate so callers know
        # the permission system is down (vs. permission denied)
        raise PermissionCheckError(
            f"Permission system unavailable while checking {permission} on {path}: {e}"
        ) from e
    except Exception as e:
        logger.error(f"Permission check failed for {path}: {e}")
        return False
