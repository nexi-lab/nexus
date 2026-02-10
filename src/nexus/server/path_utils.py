"""Path unscoping utilities for the RPC server.

Strips internal zone/tenant/user prefixes from paths before returning
them to API clients. This ensures users see clean, user-friendly paths
instead of internal storage paths.

Internal path formats handled:
    Legacy:  /tenant:{zone_id}/...                   → /...
    Legacy:  /tenant:{zone_id}/user:{user_id}/...    → /...
    Current: /zone/{zone_id}/...                      → /...
    Current: /zone/{zone_id}/user:{user_id}/...       → /...

Related: Issue #1202 - list('/') returns paths with /tenant: prefix
"""

from __future__ import annotations


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/tenant/user prefix from a storage path.

    Converts internal storage paths to user-friendly paths by removing
    the zone/tenant and user prefix segments.

    Args:
        path: Internal storage path (e.g., "/tenant:default/workspace/file.txt")

    Returns:
        User-friendly path (e.g., "/workspace/file.txt")

    Examples:
        >>> unscope_internal_path("/tenant:default/connector/gcs/file.txt")
        '/connector/gcs/file.txt'
        >>> unscope_internal_path("/zone/acme/user:alice/workspace/file.txt")
        '/workspace/file.txt'
        >>> unscope_internal_path("/workspace/file.txt")
        '/workspace/file.txt'
    """
    parts = path.lstrip("/").split("/")

    # Determine how many leading segments to skip
    skip = 0

    if parts and parts[0].startswith("tenant:"):
        # Legacy format: /tenant:{zone_id}/... or /tenant:{zone_id}/user:{user_id}/...
        skip = 1
        if len(parts) > 1 and parts[1].startswith("user:"):
            skip = 2

    elif parts and parts[0] == "zone" and len(parts) >= 2:
        # Current format: /zone/{zone_id}/... or /zone/{zone_id}/user:{user_id}/...
        skip = 2  # skip "zone" + zone_id
        if len(parts) > 2 and parts[2].startswith("user:"):
            skip = 3

    if skip == 0:
        return path if path else "/"

    remaining = "/".join(parts[skip:])
    return f"/{remaining}" if remaining else "/"


def unscope_internal_paths(paths: list[str]) -> list[str]:
    """Strip internal prefixes from a list of paths.

    Args:
        paths: List of internal storage paths

    Returns:
        List of user-friendly paths
    """
    return [unscope_internal_path(p) for p in paths]


def unscope_internal_dict(d: dict, path_keys: list[str]) -> dict:
    """Strip internal prefixes from path values in a dict.

    Creates a new dict (does not mutate the original) with specified
    keys' string values unscoped.

    Args:
        d: Dictionary potentially containing internal paths
        path_keys: Keys whose string values should be unscoped

    Returns:
        New dict with path values unscoped
    """
    result = d.copy()
    for key in path_keys:
        if key in result and isinstance(result[key], str):
            result[key] = unscope_internal_path(result[key])
    return result
