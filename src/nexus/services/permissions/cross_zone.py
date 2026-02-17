"""Cross-zone sharing constants — re-exported from rebac brick.

The canonical definition lives in ``nexus.rebac.cross_zone`` (the brick
owns its own relation semantics).  This module re-exports for callers
within ``services.permissions``.
"""

from nexus.rebac.cross_zone import CROSS_ZONE_ALLOWED_RELATIONS

__all__ = ["CROSS_ZONE_ALLOWED_RELATIONS"]
