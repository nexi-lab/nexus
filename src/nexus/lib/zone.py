"""Zone ID normalization — tier-neutral utility.

Moved from ``nexus.bricks.rebac.utils.zone`` (Issue #194) so that
kernel code can call ``normalize_zone_id`` without importing from bricks/.

Replaces the 48+ inline ``zone_id or ROOT_ZONE_ID`` occurrences with a single
canonical function so the default zone sentinel is defined in one place.
"""

from nexus.contracts.constants import ROOT_ZONE_ID

DEFAULT_ZONE: str = ROOT_ZONE_ID


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
