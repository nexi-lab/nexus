"""Zone membership composition service (Decision #8).

Composes Auth and ReBAC bricks for zone group management.
Extracted from server/auth/user_helpers.py (lines 26-337, 538-661).

These functions depend on a ReBAC manager instance and are therefore
part of the server composition layer (not a standalone brick).
"""

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ==============================================================================
# ReBAC Group Naming Helpers
# ==============================================================================


def zone_group_id(zone_id: str) -> str:
    """Generate zone group ID from zone_id.

    Example:
        zone_group_id("acme") -> "zone-acme"
    """
    return f"zone-{zone_id}"


def parse_zone_from_group(group_id: str) -> str | None:
    """Extract zone_id from group ID.

    Example:
        parse_zone_from_group("zone-acme") -> "acme"
        parse_zone_from_group("engineering") -> None
    """
    if group_id.startswith("zone-"):
        return group_id[len("zone-") :]
    return None


def is_zone_group(group_id: str) -> bool:
    """Check if group ID is a zone group (starts with "zone-")."""
    return group_id.startswith("zone-")


# ==============================================================================
# Zone Role Checks
# ==============================================================================


def is_zone_owner(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
) -> bool:
    """Check if user is owner of zone.

    Returns:
        True if user is member of group:zone-{zone_id}-owners
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

    Returns:
        True if user is member of zone-{zone_id}-admins or -owners
    """
    if is_zone_owner(rebac_manager, user_id, zone_id):
        return True

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
    """Check if user can invite others to zone (admin or owner)."""
    return is_zone_admin(rebac_manager, user_id, zone_id)


# ==============================================================================
# Zone Membership Operations
# ==============================================================================


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
    if caller_user_id:
        if role == "owner":
            if not is_zone_owner(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone owners can add other owners. "
                    f"User '{caller_user_id}' is not owner of zone '{zone_id}'"
                )
        else:
            if not can_invite_to_zone(rebac_manager, caller_user_id, zone_id):
                raise PermissionError(
                    f"Only zone admins/owners can invite users. "
                    f"User '{caller_user_id}' is not admin/owner of zone '{zone_id}'"
                )

    if role not in ("owner", "admin", "member"):
        raise ValueError(f"Invalid role '{role}'. Must be 'owner', 'admin', or 'member'")

    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"

    result = rebac_manager.rebac_write(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )
    return result.tuple_id  # type: ignore[no-any-return]


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
        role: Optional role to remove. If None, removes all roles.
    """
    if role is None:
        for r in ["owner", "admin", "member"]:
            with contextlib.suppress(Exception):
                remove_user_from_zone(rebac_manager, user_id, zone_id, r)
        return

    group_id = zone_group_id(zone_id)
    if role == "owner":
        group_id = f"{group_id}-owners"
    elif role == "admin":
        group_id = f"{group_id}-admins"

    rebac_manager.rebac_delete(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )


def get_user_zones(rebac_manager: Any, user_id: str) -> list[str]:
    """Get list of zone IDs that user belongs to.

    Uses direct DB query on rebac_tuples to find all zones where the user
    has any relation (owner, admin, member).
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
                zid = row[0] if isinstance(row, tuple | list) else row["zone_id"]
                if zid and zid not in zone_ids:
                    zone_ids.append(zid)
    except Exception as e:
        logger.debug("Zone membership lookup failed for user %s: %s", user_id, e)
    return zone_ids


def user_belongs_to_zone(rebac_manager: Any, user_id: str, zone_id: str) -> bool:
    """Check if user belongs to zone."""
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
    except Exception as e:
        logger.debug("Zone membership check failed for user=%s zone=%s: %s", user_id, zone_id, e)
        return False
