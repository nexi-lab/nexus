"""Per-zone semantic index scoping (Issue #3698).

This module holds the pure filter logic that decides whether a given
``(zone_id, virtual_path)`` pair should be fed into the embedding pipeline.
It has no I/O, no state, no DB access -- just a frozen snapshot of
``IndexScope`` plus the ``is_path_indexed`` helper.

Callers are responsible for:
    1. Loading the snapshot from the DB (``zones.indexing_mode`` +
       ``indexed_directories``) at daemon startup.
    2. Updating the snapshot on every CRUD endpoint call while holding the
       daemon's refresh lock (write-through).
    3. Canonicalizing directory paths to remove trailing slashes (except
       for '/') before inserting them into the scope.
    4. Stripping ``/zone/{zone_id}/`` prefixes from VFS paths before calling
       the helper (use ``strip_zone_prefix`` from ``mutation_events``).

The helper raises ``ValueError`` on contract violations rather than
returning False silently -- the callers catch exceptions at the loop
boundary so bugs surface loudly in logs.

The 10 matching rules are documented in tests/unit/bricks/search/
``test_index_scope.py``.  Changing the helper requires updating the test
matrix first.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "INDEX_MODE_ALL",
    "INDEX_MODE_SCOPED",
    "DirectoryAlreadyRegisteredError",
    "DirectoryNotRegisteredError",
    "IndexScope",
    "IndexScopeError",
    "IndexScopeLoadError",
    "InvalidDirectoryPathError",
    "ZoneNotFoundError",
    "canonical_directory_path",
    "is_path_indexed",
]


# =============================================================================
# Exceptions -- raised by daemon CRUD methods, translated to HTTP at the router.
# =============================================================================


class IndexScopeError(Exception):
    """Base class for index scope CRUD failures (Issue #3698)."""


class ZoneNotFoundError(IndexScopeError):
    """The target zone does not exist in the ``zones`` table."""


class InvalidDirectoryPathError(IndexScopeError):
    """The directory path is malformed, escapes the zone, or points at a file."""


class DirectoryNotRegisteredError(IndexScopeError):
    """Trying to remove a directory that was never registered."""


class DirectoryAlreadyRegisteredError(IndexScopeError):
    """Trying to register a directory that is already registered.

    Not raised when overlap is intentional (``/src`` and ``/src/lib`` can
    coexist); only raised on exact duplicate ``(zone_id, directory_path)``.
    """


class IndexScopeLoadError(IndexScopeError):
    """Failed to load index scope metadata at daemon startup.

    Raised by ``SearchDaemon._load_index_scope`` when the database read
    fails. The daemon fails closed (crashes) rather than degrading to
    ``'all'`` for every zone, which would silently disable scoped-mode
    enforcement and leak out-of-scope data.
    """


#: ``zones.indexing_mode`` value: index every file (legacy default).
INDEX_MODE_ALL = "all"

#: ``zones.indexing_mode`` value: only index files under registered directories.
INDEX_MODE_SCOPED = "scoped"


@dataclass(frozen=True)
class IndexScope:
    """Immutable snapshot of per-zone indexing scope.

    Attributes:
        zone_modes: Mapping from ``zone_id`` to mode string
            (``'all'`` or ``'scoped'``). A zone absent from this mapping
            defaults to ``'all'`` -- this is the backward-compat fallback
            for the window between daemon startup and the first zones
            query.
        zone_directories: Mapping from ``zone_id`` to a frozen set of
            canonical directory paths. Used only when the zone's mode
            is ``'scoped'``. Directory paths MUST be canonical (no
            trailing slash except for '/').
    """

    zone_modes: dict[str, str]
    zone_directories: dict[str, frozenset[str]]


def canonical_directory_path(path: str) -> str:
    """Canonicalize a directory path for storage in IndexScope.

    Rules:
        - Must start with '/'.
        - Trailing slash is stripped, except for the root '/'.
        - Consecutive slashes are collapsed.

    Raises ``ValueError`` for empty, relative, or zone-prefixed paths.
    Callers (CRUD endpoints) should use this before writing to the DB.
    """
    if not path:
        raise ValueError("directory path must not be empty")
    if not path.startswith("/"):
        raise ValueError(f"directory path must be absolute, got {path!r}")
    if path.startswith("/zone/"):
        raise ValueError(f"directory path must be canonical (no /zone/ prefix), got {path!r}")

    # Collapse '//' -> '/'. Cheap and handles common input variants.
    while "//" in path:
        path = path.replace("//", "/")

    # Strip trailing slash (except root).
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return path


def is_path_indexed(
    scope: IndexScope,
    zone_id: str,
    virtual_path: str,
) -> bool:
    """Return ``True`` if ``virtual_path`` should be fed into the embedding
    pipeline under ``zone_id`` given the current ``scope``.

    This is the canonical gate for embedding API cost control. Callers at
    every level (bootstrap, mutation consumers, refresh loop, indexing
    pipeline) should pass through this helper.

    Args:
        scope: Immutable snapshot of per-zone scope. Obtain from the
            SearchDaemon's ``_indexed_directories`` state under the
            refresh lock.
        zone_id: Non-empty zone identifier.
        virtual_path: Absolute path WITHOUT the ``/zone/{zone_id}/``
            prefix. Use ``strip_zone_prefix`` from ``mutation_events``
            to canonicalize caller input.

    Returns:
        ``True`` if the path is in scope for embedding, ``False`` otherwise.

    Raises:
        ValueError: On contract violations (empty zone, empty path,
            relative path, zone-prefixed path). These are programmer
            errors -- callers should fix the call site, not catch.
    """
    # Contract checks -- raise loudly on violations.
    if not zone_id:
        raise ValueError("zone_id must be non-empty")
    if not virtual_path:
        raise ValueError("virtual_path must not be empty")
    if not virtual_path.startswith("/"):
        raise ValueError(f"virtual_path must be absolute (start with '/'), got {virtual_path!r}")
    if virtual_path.startswith("/zone/"):
        raise ValueError(
            "virtual_path must be canonical (strip /zone/{id}/ prefix first), "
            f"got {virtual_path!r}"
        )

    # Rule 1 -- Unknown zone defaults to 'all' (backward compat fallback).
    mode = scope.zone_modes.get(zone_id, INDEX_MODE_ALL)

    # Rule 2 -- 'all' mode skips the directory check entirely.
    if mode == INDEX_MODE_ALL:
        return True

    # mode == 'scoped' from here on.
    dirs = scope.zone_directories.get(zone_id)

    # Rule 3 -- scoped with no registered dirs = index nothing.
    if not dirs:
        return False

    # Rules 4-8 -- O(depth) ancestor lookup instead of O(n) linear scan.
    #
    # Strategy: split virtual_path into its ancestor directories and test
    # each for membership in the frozenset (O(1) per test).  A realistic
    # path has at most ~10 components, so total cost is O(depth) regardless
    # of how many directories are registered.
    #
    # Exact match first (Rule 4) -- path IS a registered directory.
    if virtual_path in dirs:
        return True
    # Walk ancestor prefixes from shallowest to deepest (Rules 5/6/7/8).
    # parts[0] is always '' (absolute path starts with '/').
    parts = virtual_path.split("/")
    for depth in range(1, len(parts)):
        ancestor = "/".join(parts[:depth]) or "/"
        if ancestor in dirs:
            return True

    # No directory in the zone covered this path.
    return False
