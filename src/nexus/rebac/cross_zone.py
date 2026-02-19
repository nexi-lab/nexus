"""Cross-zone sharing constants — re-exported from rebac brick.

These constants define which relations are allowed to span zone boundaries.
Cross-zone sharing is a federation-specific policy concept
(KERNEL-ARCHITECTURE §3, federation-memo §6).
"""

from nexus.rebac.cross_zone import CROSS_ZONE_ALLOWED_RELATIONS

__all__ = ["CROSS_ZONE_ALLOWED_RELATIONS"]
