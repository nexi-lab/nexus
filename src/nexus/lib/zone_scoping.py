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
# #4005 round-2 review: ``src_path`` / ``dst_path`` cover ``sys_copy`` (and
# any future copy-shaped syscall) — without them a non-root caller's
# sys_copy would reach NexusFS with unprefixed paths and bypass the
# zone-prefix isolation guard for a mutation operation.
ZONE_PATH_ATTRS = ("path", "old_path", "new_path", "src_path", "dst_path")
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
    # #4005 round-9: collapse duplicate slashes BEFORE the prefix decision
    # so callers cannot bypass the embedded-zone check by slipping an
    # empty zone segment (``/zone//other/secret.txt`` would otherwise
    # parse with ``embedded_zone=""`` and skip the mismatch check, then
    # downstream canonicalization turns it into ``/zone/other/...``).
    canonical = path
    while "//" in canonical:
        canonical = canonical.replace("//", "/")

    if canonical.startswith("/zone/"):
        # Validate embedded zone matches caller's zone
        embedded_zone = canonical[6:].split("/", 1)[0]
        if not embedded_zone:
            raise ZoneScopingError(f"Path '{path}' has empty zone segment after /zone/ — refusing")
        if embedded_zone != caller_zone_id:
            raise ZoneScopingError(
                f"Path zone '{embedded_zone}' does not match caller zone '{caller_zone_id}'"
            )
        return canonical

    if canonical.startswith("/tenant:"):
        return canonical

    if canonical.startswith("/"):
        return f"{prefix}{canonical}"
    return f"{prefix}/{canonical}"


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

    # #4005 round-7: nested batch payloads — write_batch.files (list of
    # ``(path, content)`` tuples) and rename_batch.renames (list of
    # ``(old_path, new_path)`` tuples). Without scoping these, a tenant
    # caller could embed ``/zone/<other>/...`` paths inside a batch
    # element and bypass the mismatched-zone rejection in
    # ``scope_single_path``.
    files = getattr(params, "files", None)
    if isinstance(files, (list, tuple)):
        scoped_files: list[Any] = []
        for item in files:
            # #4005 round-8: ``files`` has two real shapes:
            #   - ``list[str]`` for GrepParams / GlobParams (path-only)
            #   - ``list[tuple[str, bytes]]`` for WriteBatchParams
            # Without scoping the str shape, a tenant caller can submit
            # ``["/zone/<other>/x"]`` to grep / glob and bypass the
            # mismatched-zone rejection.
            if isinstance(item, str):
                scoped_files.append(scope_single_path(item, prefix, zone_id))
            elif isinstance(item, (list, tuple)) and len(item) >= 1 and isinstance(item[0], str):
                scoped_files.append((scope_single_path(item[0], prefix, zone_id), *item[1:]))
            else:
                scoped_files.append(item)
        params.files = type(files)(scoped_files)

    renames = getattr(params, "renames", None)
    if isinstance(renames, (list, tuple)):
        scoped_renames = []
        for item in renames:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                old_p = (
                    scope_single_path(item[0], prefix, zone_id)
                    if isinstance(item[0], str)
                    else item[0]
                )
                new_p = (
                    scope_single_path(item[1], prefix, zone_id)
                    if isinstance(item[1], str)
                    else item[1]
                )
                scoped_renames.append((old_p, new_p, *item[2:]))
            else:
                scoped_renames.append(item)
        params.renames = type(renames)(scoped_renames)
