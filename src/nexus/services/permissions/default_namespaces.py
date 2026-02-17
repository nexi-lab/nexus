"""Default ReBAC namespace configurations — re-exported from rebac brick.

The canonical definition lives in ``nexus.rebac.default_namespaces`` (the brick
owns its own namespace semantics).  This module re-exports for callers
within ``services.permissions``.
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
