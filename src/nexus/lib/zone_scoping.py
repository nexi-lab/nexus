"""Zone-scoping helpers for RPC and gRPC request paths.

Shared module that provides a single source of truth for zone-based path
scoping — used by both the HTTP/RPC layer (``server.api.core.rpc``) and
the gRPC servicer (``grpc.servicer``).

Security (Issue #3063):
- Validates that pre-prefixed zone paths match the caller's zone.
- Prevents cross-zone path access by rejecting mismatched zone prefixes.
"""

import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

# Path attributes on RPC param dataclasses that must be zone-scoped.
ZONE_PATH_ATTRS = ("path", "old_path", "new_path")
# Bulk/list path attributes that also need zone-scoping.
ZONE_PATH_LIST_ATTRS = ("paths", "patterns")


class ZoneScopingError(ValueError):
    """Raised when a path's zone prefix doesn't match the caller's zone."""


def scope_single_path(path: str, prefix: str, caller_zone_id: str) -> str:
    """Scope a single path string with zone prefix.

    If the path is already zone-prefixed, validates that the embedded zone
    matches the caller's zone (Issue #3063 §1).

    Args:
        path: The path to scope.
        prefix: The zone prefix string, e.g. ``/zone/tenant-1``.
        caller_zone_id: The authenticated caller's zone ID.

    Returns:
        The scoped path.

    Raises:
        ZoneScopingError: If the path has a zone prefix that doesn't match
            the caller's zone.
    """
    if path.startswith("/zone/"):
        # Validate embedded zone matches caller's zone
        embedded_zone = path[6:].split("/", 1)[0]
        if embedded_zone and embedded_zone != caller_zone_id:
            raise ZoneScopingError(
                f"Path zone '{embedded_zone}' does not match caller zone '{caller_zone_id}'"
            )
        return path

    if path.startswith("/tenant:"):
        return path

    if path.startswith("/"):
        return f"{prefix}{path}"
    return f"{prefix}/{path}"


def scope_params_for_zone(params: Any, zone_id: str) -> None:
    """Prefix path attributes with ``/zone/{zone_id}/`` for zone isolation.

    Mutates the params object in place.  Only applies when *zone_id*
    differs from ROOT_ZONE_ID — the root zone sees the full tree.

    Works with both dataclass-style params (RPC) and protobuf request
    objects (gRPC).

    Args:
        params: The params/request object with path attributes.
        zone_id: The caller's authenticated zone ID.

    Raises:
        ZoneScopingError: If any path has a zone prefix that doesn't match
            the caller's zone.
    """
    if zone_id == ROOT_ZONE_ID:
        return

    prefix = f"/zone/{zone_id}"

    for attr in ZONE_PATH_ATTRS:
        value = getattr(params, attr, None)
        if not isinstance(value, str):
            continue
        setattr(params, attr, scope_single_path(value, prefix, zone_id))

    # Also scope list[str] path attributes (e.g. bulk operations)
    for attr in ZONE_PATH_LIST_ATTRS:
        value = getattr(params, attr, None)
        if not isinstance(value, list):
            continue
        setattr(
            params,
            attr,
            [scope_single_path(p, prefix, zone_id) for p in value if isinstance(p, str)],
        )
