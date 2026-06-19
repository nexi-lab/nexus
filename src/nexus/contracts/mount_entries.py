"""Pure helpers for building mount entries from ReBAC grants.

Aggregates a flat list of granted ``(object_type, object_id)`` paths into the
smallest set of mount prefixes that subsumes them — files collapse to their
parent directory; nested grants collapse to the shallowest ancestor.

No database access, no caching. Callers wanting a cached, per-subject mount
table should pair this with their own ``rebac_list_objects()`` query.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, eq=False)
class MountEntry:
    """A namespace mount entry representing a visible path for a subject.

    Visibility only — no permissions field. ReBAC handles all permission
    questions. No backend/real_path — PathRouter handles routing.
    """

    virtual_path: str

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MountEntry):
            return self.virtual_path == other.virtual_path
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.virtual_path)


def build_mount_entries(object_paths: list[tuple[str, str]]) -> list[MountEntry]:
    """Build mount entries from ReBAC-granted object paths.

    Args:
        object_paths: List of (object_type, object_id) tuples from
            ``rebac_list_objects()``. object_id is a virtual path like
            ``/workspace/project-alpha/data.csv``.

    Returns:
        Sorted list of MountEntry. Files collapse to their parent directory;
        nested grants collapse to the shallowest ancestor.
    """
    dirs: set[str] = set()

    for obj_type, obj_id in object_paths:
        if obj_type != "file":
            continue

        path = obj_id.rstrip("/")
        if not path:
            continue

        parent = os.path.dirname(path)
        if parent and parent != path:
            dirs.add(parent)
        else:
            dirs.add(path)

    sorted_dirs = sorted(dirs)
    deduplicated: list[str] = []
    for d in sorted_dirs:
        if not any(d == existing or d.startswith(existing + "/") for existing in deduplicated):
            deduplicated.append(d)

    return [MountEntry(virtual_path=d) for d in deduplicated]
