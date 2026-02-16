"""Permission expand engine.

Issue #1459 Phase 8: Extracts the Zanzibar Expand API from
ReBACManager into a focused module.

Contains:
- ExpandEngine: Find all subjects with a given permission on an object
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core.rebac import Entity

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.core.rebac import NamespaceConfig
    from nexus.rebac.tuples.repository import TupleRepository

logger = logging.getLogger(__name__)


class ExpandEngine:
    """Expands permissions to find all subjects with access.

    Implements the Zanzibar Expand API: given a permission and object,
    find all subjects that have that permission.

    Args:
        repo: TupleRepository for database access
        namespace_resolver: Callable that returns NamespaceConfig for a type
        max_depth: Maximum graph traversal depth (default 10)
    """

    def __init__(
        self,
        repo: TupleRepository,
        namespace_resolver: Callable[[str], NamespaceConfig | None],
        max_depth: int = 10,
    ) -> None:
        self._repo = repo
        self._namespace_resolver = namespace_resolver
        self._max_depth = max_depth

    def expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects with a given permission on an object.

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples

        Example:
            >>> engine.expand(
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            [("agent", "alice_id"), ("agent", "bob_id")]
        """
        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self._namespace_resolver(object_entity.entity_type)
        if not namespace:
            # No namespace - return direct relations only
            return self._repo.get_direct_subjects(permission, object_entity)

        # Recursively expand permission via namespace config
        self._expand_permission(
            permission, object_entity, namespace, subjects, visited=set(), depth=0
        )

        return list(subjects)

    def _expand_permission(
        self,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        subjects: set[tuple[str, str]],
        visited: set[tuple[str, str, str]],
        depth: int,
    ) -> None:
        """Recursively expand permission to find all subjects.

        Args:
            permission: Permission to expand
            obj: Object entity
            namespace: Namespace configuration
            subjects: Set to accumulate subjects
            visited: Set of visited (permission, object_type, object_id) to detect cycles
            depth: Current traversal depth
        """
        # Check depth limit
        if depth > self._max_depth:
            return

        # Check for cycles
        visit_key = (permission, obj.entity_type, obj.entity_id)
        if visit_key in visited:
            return
        visited.add(visit_key)

        # Get relation config
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined in namespace - check for direct relations
            direct_subjects = self._repo.get_direct_subjects(permission, obj)
            for subj in direct_subjects:
                subjects.add(subj)
            return

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                self._expand_permission(rel, obj, namespace, subjects, visited.copy(), depth + 1)
            return

        # Handle intersection (find subjects that have ALL relations)
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            if not intersection_relations:
                return

            # Get subjects for each relation
            relation_subjects = []
            for rel in intersection_relations:
                rel_subjects: set[tuple[str, str]] = set()
                self._expand_permission(
                    rel, obj, namespace, rel_subjects, visited.copy(), depth + 1
                )
                relation_subjects.append(rel_subjects)

            # Find intersection (subjects that appear in ALL sets)
            if relation_subjects:
                common_subjects = set.intersection(*relation_subjects)
                for subj in common_subjects:
                    subjects.add(subj)
            return

        # Handle exclusion
        if namespace.has_exclusion(permission):
            # Note: Expand for exclusion is complex and potentially expensive
            logger.warning(
                "Expand API does not support exclusion relations yet: %s on %s",
                permission,
                obj,
            )
            return

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all related objects
                related_objects = self._repo.find_related_objects(obj, tupleset_relation)

                # Expand permission on related objects
                for related_obj in related_objects:
                    related_ns = self._namespace_resolver(related_obj.entity_type)
                    if related_ns:
                        self._expand_permission(
                            computed_userset,
                            related_obj,
                            related_ns,
                            subjects,
                            visited.copy(),
                            depth + 1,
                        )
            return

        # Direct relation - add all subjects
        direct_subjects = self._repo.get_direct_subjects(permission, obj)
        for subj in direct_subjects:
            subjects.add(subj)
