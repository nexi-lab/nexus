"""Zone ID normalization utility.

Replaces the 48+ inline ``zone_id or "root"`` occurrences with a single
canonical function so the default zone sentinel is defined in one place.
"""


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
