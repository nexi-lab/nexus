"""Path unscoping utilities for the RPC server.

Strips internal zone/user prefixes from paths before returning
them to API clients. This ensures users see clean, user-friendly paths
instead of internal storage paths.

Internal path formats handled:
    /tenant:{zone_id}/...                   -> /...
    /tenant:{zone_id}/user:{user_id}/...    -> /...
    /zone/{zone_id}/...                      -> /...
    /zone/{zone_id}/user:{user_id}/...       -> /...

Related: Issue #1202 - list('/') returns paths with /tenant: prefix
"""

from __future__ import annotations

from typing import overload


@overload
def unscope_result(r: dict) -> dict: ...
@overload
def unscope_result(r: str) -> str: ...
@overload
def unscope_result(r: object) -> object: ...


def unscope_result(r: object) -> dict | str | object:
    """Unscope a single grep/list result (dict, str, or passthrough)."""
    if isinstance(r, dict):
        return unscope_internal_dict(r, ["path", "file"])
    if isinstance(r, str):
        return unscope_internal_path(r)
    return r


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/user prefix from a storage path.

    Converts internal storage paths to user-friendly paths by removing
    the zone/tenant and user prefix segments.

    Args:
        path: Internal storage path (e.g., "/zone/default/workspace/file.txt"
              or "/tenant:default/workspace/file.txt")

    Returns:
        User-friendly path (e.g., "/workspace/file.txt")

    Examples:
        >>> unscope_internal_path("/zone/acme/connector/gcs/file.txt")
        '/connector/gcs/file.txt'
        >>> unscope_internal_path("/zone/acme/user:alice/workspace/file.txt")
        '/workspace/file.txt'
        >>> unscope_internal_path("/tenant:default/connector/gcs/file.txt")
        '/connector/gcs/file.txt'
        >>> unscope_internal_path("/tenant:default/user:admin/workspace/file.txt")
        '/workspace/file.txt'
        >>> unscope_internal_path("/workspace/file.txt")
        '/workspace/file.txt'
    """
    parts = path.lstrip("/").split("/")

    # Determine how many leading segments to skip
    skip = 0

    if parts and parts[0].startswith("tenant:"):
        # Tenant format: /tenant:{zone_id}/... or /tenant:{zone_id}/user:{user_id}/...
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


def unscope_internal_dict(
    d: dict,
    path_keys: list[str],
) -> dict:
    """Unscope path-valued keys inside a metadata/result dict.

    Creates a shallow copy and replaces any ``path_keys`` values with
    their unscoped equivalents.

    Args:
        d: Dictionary that may contain internal storage paths.
        path_keys: List of keys whose values should be unscoped.

    Returns:
        Copy of the dict with paths unscoped.
    """
    out = dict(d)
    for key in path_keys:
        if key in out and isinstance(out[key], str):
            out[key] = unscope_internal_path(out[key])
    return out
