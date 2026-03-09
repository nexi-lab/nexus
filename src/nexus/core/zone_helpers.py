"""Zone helper functions for ReBAC-based zone membership.

These functions manage zone group naming conventions and zone membership checks
using the ReBAC (Relationship-Based Access Control) primitives. They have NO
server-layer dependencies — only rebac_manager (a core primitive).

Extracted from server/auth/user_helpers.py (Issue #1519, 3A) to eliminate
kernel→server import violations. server/auth/user_helpers.py re-exports
these for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ==============================================================================
# ReBAC Group Naming Helpers
# ==============================================================================


def zone_group_id(zone_id: str) -> str:
    """Generate zone group ID from zone_id.

    Args:
        zone_id: Zone identifier

    Returns:
        Group ID in format: zone-{zone_id}

    Example:
        zone_group_id("acme") -> "zone-acme"
    """
    return f"zone-{zone_id}"


def parse_zone_from_group(group_id: str) -> str | None:
    """Extract zone_id from group ID.

    Args:
        group_id: Group ID (e.g., "zone-acme")

    Returns:
        Zone ID or None if not a zone group

    Example:
        parse_zone_from_group("zone-acme") -> "acme"
        parse_zone_from_group("engineering") -> None
    """
    if group_id.startswith("zone-"):
        return group_id[len("zone-") :]
    return None


def is_zone_group(group_id: str) -> bool:
    """Check if group ID is a zone group.

    Args:
        group_id: Group ID to check

    Returns:
        True if group ID is a zone group (starts with "zone-")
    """
    return group_id.startswith("zone-")


def is_zone_owner(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
) -> bool:
    """Check if user is owner of zone.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID to check
        zone_id: Zone ID to check

    Returns:
        True if user is member of group:zone-{zone_id}-owners

    Example:
        if is_zone_owner(rebac_mgr, "alice", "acme"):
            # Alice can delete zone, remove any user, etc.
    """
    owner_group_id = f"{zone_group_id(zone_id)}-owners"
    return bool(
        rebac_manager.rebac_check(
            subject=("user", user_id),
            permission="member",
            object=("group", owner_group_id),
            zone_id=zone_id,
        )
    )


def is_zone_admin(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
) -> bool:
    """Check if user is admin or owner of zone.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID to check
        zone_id: Zone ID to check

    Returns:
        True if user is member of zone-{zone_id}-admins or -owners

    Example:
        if is_zone_admin(rebac_mgr, "alice", "acme"):
            # Alice can invite users, manage settings, etc.
    """
    # Check owner first (owners have all admin capabilities)
    if is_zone_owner(rebac_manager, user_id, zone_id):
        return True

    # Check admin group
    admin_group_id = f"{zone_group_id(zone_id)}-admins"
    return bool(
        rebac_manager.rebac_check(
            subject=("user", user_id),
            permission="member",
            object=("group", admin_group_id),
            zone_id=zone_id,
        )
    )


def can_invite_to_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
) -> bool:
    """Check if user can invite others to zone (admin or owner).

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID to check
        zone_id: Zone ID to check

    Returns:
        True if user is admin or owner

    Example:
        if can_invite_to_zone(rebac_mgr, "alice", "acme"):
            # Alice can call add_user_to_zone()
    """
    return is_zone_admin(rebac_manager, user_id, zone_id)


def add_user_to_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
    role: str = "member",
    caller_user_id: str | None = None,
) -> str:
    """Add user to zone via ReBAC group.

    SECURITY: Only zone admins/owners can invite users.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID to add
        zone_id: Zone ID
        role: Role in zone ("owner", "admin", or "member")
        caller_user_id: Optional user ID of caller (for permission check)

    Returns:
        ReBAC tuple ID

    Raises:
        PermissionError: If caller is not zone admin/owner
        ValueError: If role is invalid
    """
    # SECURITY: Check if caller can invite users
    if caller_user_id:
        # Only owners can add other owners
        if role == "owner":
            if not is_zone_owner(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone owners can add other owners. "
                    f"User '{caller_user_id}' is not owner of zone '{zone_id}'"
                )
        else:
            # Admins and owners can add admins/members
            if not can_invite_to_zone(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone admins/owners can invite users. "
                    f"User '{caller_user_id}' is not admin/owner of zone '{zone_id}'"
                )

    # Validate role
    if role not in ("owner", "admin", "member"):
        raise ValueError(f"Invalid role '{role}'. Must be 'owner', 'admin', or 'member'")

    # Determine group ID based on role
    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"
    # else: role == "member" -> use base group_id

    tuple_id: str = rebac_manager.rebac_write(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )
    return tuple_id


def remove_user_from_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
    role: str | None = None,
) -> None:
    """Remove user from zone via ReBAC group.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        zone_id: Zone ID
        role: Optional role to remove ("owner", "admin", or "member"). If None, removes all.

    Note:
        Removing owners should be done carefully - ensure at least one owner remains.
    """
    if role is None:
        # Remove from all groups (owner, admin, member)
        import contextlib

        for r in ["owner", "admin", "member"]:
            # Ignore errors if tuple doesn't exist
            with contextlib.suppress(Exception):
                remove_user_from_zone(rebac_manager, user_id, zone_id, r)
        return

    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"
    # else: role == "member" -> use base group_id

    # Find matching tuples via list, then delete by tuple_id
    tuples = rebac_manager.rebac_list_tuples(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
    )
    for t in tuples:
        tid = t.get("tuple_id")
        if tid:
            rebac_manager.rebac_delete(tid)


def get_user_zones(rebac_manager: Any, user_id: str) -> list[str]:
    """Get list of zone IDs that user belongs to.

    Uses direct DB query on rebac_tuples to find all zones where the user
    has any relation (owner, admin, member). Matches on the zone_id context
    column, which is set by add_user_to_zone() for all roles.

    Args:
        rebac_manager: ReBAC manager instance (must have _connection())
        user_id: User ID

    Returns:
        List of zone IDs
    """
    zone_ids: list[str] = []
    try:
        with rebac_manager._connection() as conn:
            cursor = conn.cursor() if hasattr(conn, "cursor") else conn
            cursor.execute(
                "SELECT DISTINCT zone_id FROM rebac_tuples "
                "WHERE subject_type = 'user' AND subject_id = ? "
                "AND zone_id IS NOT NULL",
                (user_id,),
            )
            for row in cursor.fetchall():
                zid = row[0] if isinstance(row, (tuple, list)) else row["zone_id"]
                if zid and zid not in zone_ids:
                    zone_ids.append(zid)
    except Exception as e:
        logger.warning("Failed to fetch zone IDs for user %s: %s", user_id, e)
    return zone_ids


def user_belongs_to_zone(rebac_manager: Any, user_id: str, zone_id: str) -> bool:
    """Check if user belongs to zone.

    Checks rebac_tuples for any tuple where the user is the subject and the
    zone_id context column matches. This covers all roles (owner, admin,
    member) since add_user_to_zone() always sets zone_id on the tuple.

    Args:
        rebac_manager: ReBAC manager instance
        user_id: User ID
        zone_id: Zone ID

    Returns:
        True if user belongs to zone
    """
    try:
        with rebac_manager._connection() as conn:
            cursor = conn.cursor() if hasattr(conn, "cursor") else conn
            cursor.execute(
                "SELECT 1 FROM rebac_tuples "
                "WHERE subject_type = 'user' AND subject_id = ? "
                "AND zone_id = ? "
                "LIMIT 1",
                (user_id, zone_id),
            )
            return cursor.fetchone() is not None
    except Exception:
        return False
