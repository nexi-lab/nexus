"""Shared utility functions for cache normalization.

Issue #3192: Centralizes cache normalization logic to prevent inconsistencies
across cache layers (boundary cache, enforcer cache, result cache, etc.).
All cache layers should use these functions instead of implementing their own
normalization, ensuring consistent key generation and lookup behavior.
"""

from nexus.contracts.constants import ROOT_ZONE_ID


def normalize_path(path: str) -> str:
    """Canonical path normalization used by all cache layers.

    - Root path "/" stays as "/"
    - Empty or None becomes "/"
    - Strips trailing slashes (except root)
    - Collapses double slashes
    """
    if not path:
        return "/"
    # Collapse repeated slashes (e.g., "//a///b" -> "/a/b")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    normalized = "/" + "/".join(parts)
    return normalized


def normalize_zone_id(zone_id: str | None) -> str:
    """Normalize zone ID for consistent cache keying.

    None or empty string becomes ROOT_ZONE_ID ("root").
    """
    if not zone_id:
        return ROOT_ZONE_ID
    return zone_id


def get_ancestor_paths(path: str) -> list[str]:
    """Get all ancestor paths from immediate parent to root.

    Always includes root "/". Uses normalize_path internally.

    Examples:
        get_ancestor_paths("/a/b/c") -> ["/a/b", "/a", "/"]
        get_ancestor_paths("/a") -> ["/"]
        get_ancestor_paths("/") -> []
    """
    normalized = normalize_path(path)
    if normalized == "/":
        return []

    ancestors = []
    current = normalized
    while True:
        parent = current.rsplit("/", 1)[0]
        if not parent:
            parent = "/"
        ancestors.append(parent)
        if parent == "/":
            break
        current = parent
    return ancestors
