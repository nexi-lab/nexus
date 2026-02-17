"""Zone management helper functions.

Utilities for creating, validating, and managing zones.
"""

import re

from sqlalchemy.orm import Session

from nexus.storage.models import ZoneModel

# Reserved zone_id values that cannot be used
RESERVED_ZONE_IDS = {
    # System identifiers
    "admin",
    "system",
    "default",
    "zone",
    "user",
    "agent",
    "group",
    "root",
    "nexus",
    # Common routes/endpoints
    "api",
    "auth",
    "oauth",
    "login",
    "signup",
    "register",
    "logout",
    "callback",
    "health",
    "status",
    "docs",
    "swagger",
    # Reserved for future use
    "settings",
    "billing",
    "support",
    "help",
    "pricing",
    "features",
}


def validate_zone_id(zone_id: str) -> tuple[bool, str | None]:
    """Validate zone_id format and check for reserved names.

    Rules:
    - Length: 3-63 characters
    - Format: lowercase alphanumeric + hyphens
    - Cannot start or end with hyphen
    - Cannot be a reserved name

    Args:
        zone_id: Zone identifier to validate

    Returns:
        Tuple of (is_valid, error_message)

    Example:
        >>> validate_zone_id("acme")
        (True, None)
        >>> validate_zone_id("admin")
        (False, "Zone ID 'admin' is reserved")
        >>> validate_zone_id("a")
        (False, "Zone ID must be 3-63 characters")
    """
    # Check length
    if len(zone_id) < 3:
        return False, "Zone ID must be at least 3 characters"
    if len(zone_id) > 63:
        return False, "Zone ID must be 63 characters or less"

    # Check format: lowercase alphanumeric + hyphens, cannot start/end with hyphen
    pattern = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"
    if not re.match(pattern, zone_id):
        return (
            False,
            "Zone ID must be lowercase alphanumeric (a-z, 0-9) with hyphens, "
            "and cannot start or end with a hyphen",
        )

    # Check reserved names
    if zone_id in RESERVED_ZONE_IDS:
        return False, f"Zone ID '{zone_id}' is reserved"

    return True, None


def is_zone_id_available(session: Session, zone_id: str) -> bool:
    """Check if zone_id is available (not already taken).

    Args:
        session: Database session
        zone_id: Zone identifier to check

    Returns:
        True if available, False if already taken
    """
    existing = session.get(ZoneModel, zone_id)
    return existing is None


def suggest_zone_id(base_name: str, session: Session) -> str:
    """Suggest an available zone_id based on a base name.

    If base_name is taken, tries appending numbers (base-2, base-3, etc.)
    until an available ID is found.

    Args:
        base_name: Desired base name (e.g., "acme" or "Acme Corp")
        session: Database session

    Returns:
        Available zone_id

    Example:
        >>> suggest_zone_id("Acme Corp", session)
        "acme-corp"  # if available
        >>> suggest_zone_id("acme", session)
        "acme-2"  # if "acme" is taken
    """
    # Normalize base name to slug format
    zone_id = normalize_to_slug(base_name)

    # If normalized slug is available, return it
    if is_zone_id_available(session, zone_id):
        return zone_id

    # Try appending numbers
    counter = 2
    while counter < 1000:  # Reasonable limit
        candidate = f"{zone_id}-{counter}"
        if is_zone_id_available(session, candidate):
            return candidate
        counter += 1

    # Fallback: use timestamp
    import time

    return f"{zone_id}-{int(time.time())}"


def normalize_to_slug(name: str) -> str:
    """Normalize a name to a valid zone_id slug format.

    Converts to lowercase, replaces spaces and special characters with hyphens,
    removes consecutive hyphens, and trims to valid length.

    Args:
        name: Original name (e.g., "Acme Corporation")

    Returns:
        Normalized slug (e.g., "acme-corporation")

    Example:
        >>> normalize_to_slug("Acme Corporation")
        "acme-corporation"
        >>> normalize_to_slug("Tech@Startup!!!  Inc.")
        "tech-startup-inc"
    """
    # Convert to lowercase
    slug = name.lower()

    # Replace spaces and special characters with hyphens
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)

    # Remove consecutive hyphens
    slug = re.sub(r"-+", "-", slug)

    # Remove leading/trailing hyphens
    slug = slug.strip("-")

    # Ensure minimum length (pad with numbers if needed)
    if len(slug) < 3:
        slug = f"{slug}-org"

    # Trim to maximum length
    if len(slug) > 63:
        slug = slug[:63].rstrip("-")

    return slug


def create_zone(
    session: Session,
    zone_id: str,
    name: str,
    domain: str | None = None,
    description: str | None = None,
    settings: str | None = None,
) -> ZoneModel:
    """Create a new zone with validation.

    Args:
        session: Database session
        zone_id: Desired zone identifier (validated before creation)
        name: Display name for the zone
        domain: Optional domain (e.g., "acme.com")
        description: Optional description
        settings: Optional JSON settings

    Returns:
        Created ZoneModel

    Raises:
        ValueError: If zone_id is invalid or already taken
    """
    # Validate zone_id format
    is_valid, error_msg = validate_zone_id(zone_id)
    if not is_valid:
        raise ValueError(f"Invalid zone_id: {error_msg}")

    # Check availability
    if not is_zone_id_available(session, zone_id):
        raise ValueError(f"Zone ID '{zone_id}' is already taken")

    # Create zone
    from datetime import UTC, datetime

    zone = ZoneModel(
        zone_id=zone_id,
        name=name,
        domain=domain,
        description=description,
        settings=settings,
        is_active=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    session.add(zone)
    session.commit()

    return zone
