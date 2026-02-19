"""Default ReBAC namespace configurations — re-exported from rebac brick.

These define the default permission schemas for each object type
(file, group, memory, playbook, trajectory, skill).  The canonical
definition lives in the rebac brick since it owns namespace semantics.

Canonical import:
    from nexus.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE
"""

from nexus.rebac.default_namespaces import (
    DEFAULT_FILE_NAMESPACE,
    DEFAULT_GROUP_NAMESPACE,
    DEFAULT_MEMORY_NAMESPACE,
    DEFAULT_PLAYBOOK_NAMESPACE,
    DEFAULT_SKILL_NAMESPACE,
    DEFAULT_TRAJECTORY_NAMESPACE,
)

__all__ = [
    "DEFAULT_FILE_NAMESPACE",
    "DEFAULT_GROUP_NAMESPACE",
    "DEFAULT_MEMORY_NAMESPACE",
    "DEFAULT_PLAYBOOK_NAMESPACE",
    "DEFAULT_SKILL_NAMESPACE",
    "DEFAULT_TRAJECTORY_NAMESPACE",
]
