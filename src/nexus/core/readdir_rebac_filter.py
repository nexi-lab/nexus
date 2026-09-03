"""ReBAC post-filter for ``sys_readdir`` results (Issue #4739).

``sys_readdir`` used to apply zone filtering only, so a non-admin key could
enumerate the names of files it cannot read (the HTTP ``/files/list`` route
calls the syscall directly).  Files must now pass
``PermissionEnforcer.filter_list`` — the same chain search ``list`` / ``glob``
/ ``grep`` use, honouring ``OperationContext.consistency`` — and directories
stay visible when the caller has an accessible descendant
(``has_accessible_descendants_batch``), matching ``SearchService``.

Kept out of ``nexus_fs_metadata.py`` so that module stays under the
repository's file-size limit; ``MetadataMixin.sys_readdir`` calls
:func:`readdir_visible_paths` with itself as ``fs``.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def readdir_filter_context(context: Any) -> OperationContext | None:
    """Return the caller identity the ReBAC listing filter applies to.

    ``None`` means "do not filter": no caller identity (kernel-internal and
    embedded callers), or an admin / system caller — their listing semantics
    are unchanged here (admin zone scoping is tracked in #4740).  Dict
    contexts are promoted to ``OperationContext`` when they carry a subject
    so RPC callers cannot sidestep the filter.
    """
    if context is None:
        return None
    if isinstance(context, dict):
        if context.get("is_admin") or context.get("is_system"):
            return None
        user = context.get("user_id") or context.get("subject_id")
        if not user:
            return None
        return OperationContext(
            user_id=str(user),
            groups=list(context.get("groups") or []),
            zone_id=context.get("zone_id"),
            subject_type=str(context.get("subject_type") or "user"),
            subject_id=context.get("subject_id"),
            consistency=context.get("consistency"),
        )
    if getattr(context, "is_admin", False) or getattr(context, "is_system", False):
        return None
    return context if isinstance(context, OperationContext) else None


def readdir_visible_paths(
    fs: Any,
    entries: list[tuple[str, bool]],
    context: Any,
) -> set[str] | None:
    """Return the paths of *entries* the caller may see, or ``None`` to skip filtering.

    Args:
        fs: The ``NexusFS`` (``MetadataMixin``) instance — supplies
            ``_perm_config`` and the ``permission_enforcer`` service.
        entries: ``(path, is_directory)`` pairs of the candidate result.
        context: Caller context (``OperationContext`` or dict).

    Returns:
        The set of visible paths, or ``None`` when no filtering applies (no
        identity, admin/system caller, enforcement disabled).  Fails closed
        (empty set) when enforcement is on but the enforcer is unavailable,
        like ``PermissionChecker.check`` does for reads.
    """
    ctx = readdir_filter_context(context)
    if ctx is None or not entries:
        return None
    perm = getattr(fs, "_perm_config", None)
    if perm is None or not getattr(perm, "enforce", False):
        return None
    service = getattr(fs, "service", None)
    enforcer = service("permission_enforcer") if callable(service) else None
    if enforcer is None:
        logger.warning(
            "sys_readdir: permission enforcer unavailable; hiding %d entries (fail-closed)",
            len(entries),
        )
        return set()
    files = [p for p, is_dir in entries if not is_dir]
    dirs = [p for p, is_dir in entries if is_dir]
    visible: set[str] = set(enforcer.filter_list(files, ctx)) if files else set()
    if dirs:
        dir_visibility = enforcer.has_accessible_descendants_batch(dirs, ctx)
        visible.update(d for d in dirs if dir_visibility.get(d, True))
    return visible
