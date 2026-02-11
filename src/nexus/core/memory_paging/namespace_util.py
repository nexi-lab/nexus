"""Shared utility for tier namespace manipulation."""

from __future__ import annotations

# Tier namespace prefixes to strip when moving memories between tiers
_TIER_PREFIXES = ("recall/", "archival/")


def strip_tier_prefix(namespace: str | None) -> str:
    """Extract the base namespace by stripping tier prefixes.

    Removes the leading ``recall/`` or ``archival/`` prefix so a memory's
    logical namespace can be re-prefixed for its destination tier.

    Only strips the outermost prefix (``recall/archival/foo`` -> ``archival/foo``).

    Args:
        namespace: Raw namespace (may be None or already have tier prefix).

    Returns:
        Base namespace with the tier prefix removed.
    """
    base = namespace or "default"
    for prefix in _TIER_PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break  # Only strip the outermost prefix
    return base
