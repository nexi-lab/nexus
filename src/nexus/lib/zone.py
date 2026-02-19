"""Zone ID normalization — tier-neutral utility.

Moved from ``nexus.services.permissions.utils.zone`` (Issue #194) so that
kernel code can call ``normalize_zone_id`` without importing from services/.
"""

from __future__ import annotations

DEFAULT_ZONE: str = "root"


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
