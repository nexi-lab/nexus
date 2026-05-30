"""Zone validation and management helpers for the Auth brick.

Moved from server/auth/zone_helpers.py. Pure functions for zone
validation, creation, and email domain classification.
"""

import re

from sqlalchemy.orm import Session

from nexus.bricks.auth.constants import RESERVED_ZONE_IDS
from nexus.contracts.zone_phase import ZonePhase
from nexus.storage.models import ZoneModel


def validate_zone_id(zone_id: str) -> tuple[bool, str | None]:
    """Validate zone_id format and check for reserved names.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if len(zone_id) < 3:
        return False, "Zone ID must be at least 3 characters"
    if len(zone_id) > 63:
        return False, "Zone ID must be 63 characters or less"

    pattern = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"
    if not re.match(pattern, zone_id):
        return (
            False,
            "Zone ID must be lowercase alphanumeric (a-z, 0-9) with hyphens, "
            "and cannot start or end with a hyphen",
        )

    if zone_id in RESERVED_ZONE_IDS:
        return False, f"Zone ID '{zone_id}' is reserved"

    return True, None


def is_zone_id_available(session: Session, zone_id: str) -> bool:
    """Check if zone_id is available (not already taken)."""
    existing = session.get(ZoneModel, zone_id)
    return existing is None


def suggest_zone_id(base_name: str, session: Session) -> str:
    """Suggest an available zone_id based on a base name."""
    zone_id = normalize_to_slug(base_name)

    if is_zone_id_available(session, zone_id):
        return zone_id

    counter = 2
    while counter < 1000:
        candidate = f"{zone_id}-{counter}"
        if is_zone_id_available(session, candidate):
            return candidate
        counter += 1

    import time

    return f"{zone_id}-{int(time.time())}"


def normalize_to_slug(name: str) -> str:
    """Normalize a name to a valid zone_id slug format."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")

    if len(slug) < 3:
        slug = f"{slug}-org"

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

    Raises:
        ValueError: If zone_id is invalid or already taken.
    """
    is_valid, error_msg = validate_zone_id(zone_id)
    if not is_valid:
        raise ValueError(f"Invalid zone_id: {error_msg}")

    if not is_zone_id_available(session, zone_id):
        raise ValueError(f"Zone ID '{zone_id}' is already taken")

    from datetime import UTC, datetime

    zone = ZoneModel(
        zone_id=zone_id,
        name=name,
        domain=domain,
        description=description,
        settings=settings,
        phase=ZonePhase.ACTIVE,
        finalizers="[]",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    session.add(zone)
    session.commit()

    return zone
