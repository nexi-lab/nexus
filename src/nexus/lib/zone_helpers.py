"""Zone helper functions for ReBAC-based zone membership.

These functions manage zone group naming conventions and zone membership checks
using the ReBAC (Relationship-Based Access Control) primitives. They have NO
kernel-layer dependencies — callers pass a ``rebac_manager`` (duck-typed to
``ReBACBrickProtocol`` from ``nexus.services.protocols``) via dependency injection.

All functions use Protocol methods exclusively — no private attribute access.

Tier-neutral utility — ``lib/`` (zero kernel deps).
``rebac_manager: Any`` params accept any object satisfying
``nexus.contracts.protocols.rebac.ReBACBrickProtocol``.
"""

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
) -> Any:
    """Add user to zone via ReBAC group.

    SECURITY: Only zone admins/owners can invite users.

    Args:
        rebac_manager: ReBAC manager (Protocol-typed)
        user_id: User ID to add
        zone_id: Zone ID
        role: Role in zone ("owner", "admin", or "member")
        caller_user_id: Optional user ID of caller (for permission check)

    Returns:
        WriteResult from rebac_write (contains tuple_id)

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
        elif not can_invite_to_zone(rebac_manager, caller_user_id, zone_id):
            raise PermissionError(
                f"Only zone admins/owners can invite users. "
                f"User '{caller_user_id}' is not admin/owner of zone '{zone_id}'"
            )

    if role not in ("owner", "admin", "member"):
        raise ValueError(f"Invalid role '{role}'. Must be 'owner', 'admin', or 'member'")

    group_id = _role_group_id(zone_id, role)
    return rebac_manager.rebac_write(
        subject=("user", user_id),
        relation="member",
        object=("group", group_id),
        zone_id=zone_id,
    )


def remove_user_from_zone(
    rebac_manager: Any,
    user_id: str,
    zone_id: str,
    role: str | None = None,
) -> None:
    """Remove user from zone via ReBAC group.

    Uses ``rebac_list_tuples`` to find the tuple ID, then ``rebac_delete``
    to remove it.  This avoids private ``_connection()`` access and uses
    the Protocol-defined cache-aware path.

    Args:
        rebac_manager: ReBAC manager (Protocol-typed)
        user_id: User ID
        zone_id: Zone ID
        role: Optional role to remove ("owner", "admin", or "member"). If None, removes all.

    Note:
        Removing owners should be done carefully - ensure at least one owner remains.
    """
    if role is None:
        errors: list[Exception] = []
        for r in ["owner", "admin", "member"]:
            try:
                remove_user_from_zone(rebac_manager, user_id, zone_id, r)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup(
                f"Partial failure removing user {user_id} from zone {zone_id}",
                errors,
            )
        return

    group_id = _role_group_id(zone_id, role)

    # Find matching tuples via Protocol method
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

    Uses ``rebac_list_tuples`` to find all tuples where the user is the
    subject, then extracts distinct zone IDs.  This replaces the previous
    raw SQL approach and uses the Protocol-defined cache-aware path.

    Args:
        rebac_manager: ReBAC manager (Protocol-typed)
        user_id: User ID

    Returns:
        List of zone IDs (deduplicated, stable order)
    """
    zone_ids: list[str] = []
    try:
        tuples = rebac_manager.rebac_list_tuples(
            subject=("user", user_id),
            relation="member",
        )
        seen: set[str] = set()
        for t in tuples:
            zid = t.get("zone_id")
            if zid and zid not in seen:
                seen.add(zid)
                zone_ids.append(zid)
    except Exception as e:
        logger.warning("Failed to fetch zone IDs for user %s: %s", user_id, e)
    return zone_ids


def user_belongs_to_zone(rebac_manager: Any, user_id: str, zone_id: str) -> bool:
    """Check if user belongs to zone (any role).

    Uses ``rebac_check`` for each possible group (member, admin, owner)
    to determine zone membership via the Protocol-defined cache-aware path.

    Args:
        rebac_manager: ReBAC manager (Protocol-typed)
        user_id: User ID
        zone_id: Zone ID

    Returns:
        True if user belongs to zone in any role
    """
    subject = ("user", user_id)
    for role in ("member", "admin", "owner"):
        group_id = _role_group_id(zone_id, role)
        if rebac_manager.rebac_check(
            subject=subject,
            permission="member",
            object=("group", group_id),
            zone_id=zone_id,
        ):
            return True
    return False


# ==============================================================================
# Internal Helpers
# ==============================================================================


def _role_group_id(zone_id: str, role: str) -> str:
    """Map (zone_id, role) to the ReBAC group ID.

    Args:
        zone_id: Zone identifier
        role: "owner", "admin", or "member"

    Returns:
        Group ID (e.g., "zone-acme-owners" or "zone-acme" for member)
    """
    group_id = zone_group_id(zone_id)
    if role == "owner":
        return f"{group_id}-owners"
    if role == "admin":
        return f"{group_id}-admins"
    return group_id
