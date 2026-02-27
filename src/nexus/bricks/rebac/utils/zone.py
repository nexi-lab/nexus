"""Zone ID normalization and ReBAC zone group utilities.

Provides:
- normalize_zone_id: re-exported from nexus.lib.zone (canonical location)
- Zone group naming helpers: zone_group_id, parse_zone_from_group, is_zone_group
- Zone role checks: is_zone_owner, is_zone_admin, can_invite_to_zone
"""

from typing import Any

from nexus.lib.zone import normalize_zone_id  # noqa: F401

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
    """Check if user is owner of zone."""
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
    """Check if user is admin or owner of zone."""
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
