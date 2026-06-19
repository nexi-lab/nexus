"""Shared operator dispatch for ReBAC graph traversal.

Extracts the union/intersection/exclusion dispatch logic that was
independently implemented in bulk_evaluator.py, traversal.py, and
zone_traversal.py into a single SSOT module.

Two dispatch levels:
- ``dispatch_permission_operators``: permission-level dispatch that
  inspects the raw ``perm_def`` dict/list from namespace config.
- ``dispatch_relation_operators``: relation-level dispatch via
  ``namespace.has_union/has_intersection/has_exclusion``.

Both include fail-closed validation for empty/invalid operands
(rounds 5-7 security hardening).

Related: PR #4243 follow-up, DRY-1 audit finding
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import NamespaceConfig

logger = logging.getLogger(__name__)


def _all_nonempty_strings(items: Any) -> bool:
    """Validate that items is a non-empty list of non-empty strings."""
    return (
        isinstance(items, list) and len(items) > 0 and all(isinstance(m, str) and m for m in items)
    )


def dispatch_permission_operators(
    perm_def: Any,
    permission: str,
    obj_entity_type: str,
    recurse: Callable[[str], bool],
) -> bool | None:
    """Dispatch permission-level operators from raw namespace config.

    Inspects the ``perm_def`` value (list or dict with union/intersection/
    exclusion keys) and applies correct OR/AND/NOT semantics.

    Returns True/False if an operator matched, None if ``perm_def`` is
    not a recognized shape (caller should fail closed).
    """
    if isinstance(perm_def, list):
        if not _all_nonempty_strings(perm_def):
            logger.warning(
                "dispatch_permission_operators: empty/invalid list for '%s' in %s; failing closed",
                permission,
                obj_entity_type,
            )
            return False
        return any(recurse(userset) for userset in perm_def)

    if isinstance(perm_def, dict):
        if "union" in perm_def:
            if not _all_nonempty_strings(perm_def["union"]):
                logger.warning(
                    "dispatch_permission_operators: empty/invalid union for '%s' in %s; "
                    "failing closed",
                    permission,
                    obj_entity_type,
                )
                return False
            return any(recurse(userset) for userset in perm_def["union"])

        if "intersection" in perm_def:
            if not _all_nonempty_strings(perm_def["intersection"]):
                logger.warning(
                    "dispatch_permission_operators: empty/invalid intersection for '%s' "
                    "in %s; failing closed",
                    permission,
                    obj_entity_type,
                )
                return False
            return all(recurse(userset) for userset in perm_def["intersection"])

        if "exclusion" in perm_def:
            exclusion_target = perm_def.get("exclusion")
            if not isinstance(exclusion_target, str) or not exclusion_target:
                logger.warning(
                    "dispatch_permission_operators: empty/invalid exclusion for '%s' "
                    "in %s; failing closed",
                    permission,
                    obj_entity_type,
                )
                return False
            return not recurse(exclusion_target)

        # Unknown dict operator — signal unrecognized so caller can
        # fail closed with appropriate logging.
        return None

    # Unrecognized shape (not list, not dict).
    return None


def dispatch_relation_operators(
    namespace: NamespaceConfig,
    permission: str,
    obj_entity_type: str,
    recurse: Callable[[str], bool],
) -> bool | None:
    """Dispatch relation-level union/intersection/exclusion operators.

    Checks ``namespace.has_union/has_intersection/has_exclusion`` and
    applies correct OR/AND/NOT semantics with fail-closed validation.

    Returns True/False if an operator matched, None if none matched
    (caller should fall through to tupleToUserset or direct relation).
    """
    if namespace.has_union(permission):
        union_relations = namespace.get_union_relations(permission)
        if not _all_nonempty_strings(union_relations):
            logger.warning(
                "dispatch_relation_operators: empty/invalid union for '%s' in %s; failing closed",
                permission,
                obj_entity_type,
            )
            return False
        return any(recurse(rel) for rel in union_relations)

    if namespace.has_intersection(permission):
        intersection_relations = namespace.get_intersection_relations(permission)
        if not _all_nonempty_strings(intersection_relations):
            logger.warning(
                "dispatch_relation_operators: empty/invalid intersection for '%s' "
                "in %s; failing closed",
                permission,
                obj_entity_type,
            )
            return False
        return all(recurse(rel) for rel in intersection_relations)

    if namespace.has_exclusion(permission):
        excluded_rel = namespace.get_exclusion_relation(permission)
        if not isinstance(excluded_rel, str) or not excluded_rel:
            logger.warning(
                "dispatch_relation_operators: empty/invalid exclusion for '%s' "
                "in %s; failing closed",
                permission,
                obj_entity_type,
            )
            return False
        return not recurse(excluded_rel)

    return None


def parent_path_of(file_path: str) -> str | None:
    """Compute the POSIX parent path, returning None for roots.

    >>> parent_path_of("/foo/bar")
    '/foo'
    >>> parent_path_of("/") is None
    True
    >>> parent_path_of(".") is None
    True
    """
    parent = str(PurePosixPath(file_path).parent)
    if parent == file_path or parent == ".":
        return None
    return parent
