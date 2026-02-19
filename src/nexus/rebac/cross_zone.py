"""Cross-zone sharing constants for ReBAC federation.

These constants define which relations are allowed to span zone boundaries.
Cross-zone sharing is a federation-specific policy concept
(KERNEL-ARCHITECTURE §3, federation-memo §6).
"""

# Relations that are allowed to cross zone boundaries.
# These relations can link subjects and objects from different zones.
CROSS_ZONE_ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {
        "shared-viewer",  # Read access via cross-zone share
        "shared-editor",  # Read + Write access via cross-zone share
        "shared-owner",  # Full access via cross-zone share
    }
)
