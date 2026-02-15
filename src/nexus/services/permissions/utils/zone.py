"""Zone ID normalization utility.

Replaces the 48+ inline ``zone_id or "default"`` occurrences with a single
canonical function so the default zone sentinel is defined in one place.
"""

from __future__ import annotations

DEFAULT_ZONE: str = "default"


def normalize_zone_id(zone_id: str | None) -> str:
    """Return *zone_id* if truthy, otherwise the default zone sentinel.

    >>> normalize_zone_id("tenant-1")
    'tenant-1'
    >>> normalize_zone_id(None)
    'default'
    >>> normalize_zone_id("")
    'default'
    """
    return zone_id or DEFAULT_ZONE
