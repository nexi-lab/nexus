"""Recursive ``sys_readdir`` expansion into directories behind their own route.

Split out of :mod:`nexus.core.nexus_fs_metadata` (file-size limit); the
mixin's ``_expand_recursive_readdir`` delegates here.
"""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.metadata import DT_DIR, DT_MOUNT

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def expand_recursive_readdir(
    fs: Any,
    entries: builtins.list[Any],
    *,
    details: bool,
    context: OperationContext | None,
    all_zones: bool = False,
    entry_types: dict[str, int] | None = None,
) -> builtins.list[Any]:
    """Expand explicit child directories that live behind their own metastore route.

    ``all_zones`` is forwarded to the nested ``sys_readdir`` calls so an
    admin's explicit cross-zone listing (#4740) keeps the same view while
    descending into child routes.

    Only directories that can hide entries are descended into: a mount
    (its own route) always, a plain directory only while the listing
    holds none of its descendants — one that has descendants was covered
    by the same prefix scan.  Directory-ness comes from ``entry_types``
    (metastore entry type per bare-string entry) or a detail dict's
    ``entry_type``, and the nested listings are fetched with details, so
    no entry needs a ``sys_stat``.  Before this a recursive listing of
    ``/`` stat-ed every entry and re-listed every directory's subtree:
    ~34 min for ~150k files on a production zone, paid on every boot by
    the Tiger resource-map sync.
    """
    from collections import deque

    by_path: dict[str, Any] = {}
    mounts: set[str] = set()
    has_descendants: set[str] = set()
    pending_dirs: deque[str] = deque()

    def entry_type_of(item: Any, path: str) -> int | None:
        if isinstance(item, dict):
            return item.get("entry_type")
        return entry_types.get(path) if entry_types is not None else None

    def remember(item: Any, stored: Any) -> None:
        path = fs._readdir_item_path(item)
        if path is None or path in by_path:
            return
        by_path[path] = stored
        parent = path.rsplit("/", 1)[0]
        while parent and parent not in has_descendants:
            has_descendants.add(parent)
            parent = parent.rsplit("/", 1)[0]
        entry_type = entry_type_of(item, path)
        if entry_type is not None or isinstance(item, dict):
            is_dir = entry_type in (DT_DIR, DT_MOUNT) or bool(
                isinstance(item, dict) and item.get("is_directory")
            )
        else:
            is_dir = fs._readdir_item_is_dir(item, context=context)
        if entry_type == DT_MOUNT:
            mounts.add(path)
        if is_dir:
            pending_dirs.append(path)

    def needs_expansion(directory: str) -> bool:
        return directory in mounts or directory not in has_descendants

    for entry in entries:
        remember(entry, entry)

    max_entries = 100_000
    expanded: set[str] = set()
    while pending_dirs and len(by_path) < max_entries:
        directory = pending_dirs.popleft()
        if directory in expanded or not needs_expansion(directory):
            continue
        expanded.add(directory)
        try:
            # Detail dicts carry entry_type, so the children need no stat.
            child_entries = fs.sys_readdir(
                directory,
                recursive=True,
                details=True,
                context=context,
                all_zones=all_zones,
            )
        except Exception as exc:
            logger.debug("recursive sys_readdir expansion skipped for %s: %s", directory, exc)
            continue
        for child in child_entries:
            remember(child, child if details else fs._readdir_item_path(child))

    if any(d not in expanded and needs_expansion(d) for d in pending_dirs):
        logger.warning(
            "recursive sys_readdir expansion truncated at %d entries under explicit dirs",
            max_entries,
        )

    return [by_path[path] for path in sorted(by_path)]
