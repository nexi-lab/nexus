"""Cross-zone sharing constants for ReBAC federation.

Backward-compat shim (Issue #2190): Canonical location is
``nexus.contracts.rebac_types``. This module re-exports for existing importers.

These constants define which relations are allowed to span zone boundaries.
Cross-zone sharing is a federation-specific policy concept
(KERNEL-ARCHITECTURE §3, federation-memo §6).
"""

from nexus.contracts.rebac_types import (  # noqa: F401
    CROSS_ZONE_ALLOWED_RELATIONS as CROSS_ZONE_ALLOWED_RELATIONS,
)
