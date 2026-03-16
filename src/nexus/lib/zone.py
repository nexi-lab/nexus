"""Zone ID normalization and validation — tier-neutral utility.

Moved from ``nexus.bricks.rebac.utils.zone`` (Issue #194) so that
kernel code can call ``normalize_zone_id`` without importing from bricks/.

Replaces the 48+ inline ``zone_id or ROOT_ZONE_ID`` occurrences with a single
canonical function so the default zone sentinel is defined in one place.
"""

import re

from nexus.contracts.constants import ROOT_ZONE_ID

DEFAULT_ZONE: str = ROOT_ZONE_ID

# Zone IDs must be alphanumeric, hyphens, underscores, or dots (no path separators).
_SAFE_ZONE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def normalize_zone_id(zone_id: str | None) -> str:
    """Return *zone_id* if truthy, otherwise the default zone sentinel.

    >>> normalize_zone_id("tenant-1")
    'tenant-1'
    >>> normalize_zone_id(None)
    'root'
    >>> normalize_zone_id("")
    'root'
    """
    return zone_id or DEFAULT_ZONE


def validate_zone_id(zone_id: str) -> str:
    """Validate zone_id is safe for use in filesystem paths.

    Rejects path traversal sequences (``..``, ``/``, ``\\``) and other
    unsafe characters that could escape a cache root directory.

    Args:
        zone_id: Zone identifier to validate.

    Returns:
        The validated zone_id (unchanged).

    Raises:
        ValueError: If zone_id contains unsafe characters.
    """
    if not zone_id:
        raise ValueError("zone_id must not be empty")
    if not _SAFE_ZONE_RE.match(zone_id):
        raise ValueError(
            f"zone_id contains unsafe characters: {zone_id!r}. "
            "Only alphanumeric, hyphens, underscores, and dots are allowed."
        )
    if zone_id.startswith(".") or ".." in zone_id:
        raise ValueError(f"zone_id must not start with '.' or contain '..': {zone_id!r}")
    return zone_id
