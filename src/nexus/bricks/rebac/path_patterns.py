"""Helpers for explicit ReBAC file path-pattern tuple object IDs."""

from __future__ import annotations

from nexus.bricks.rebac._path_utils import get_ancestors, get_parent

RECURSIVE_SUFFIX = "/**"
SINGLE_LEVEL_SUFFIX = "/*"


def _is_absolute_file_path(object_type: str, object_id: str) -> bool:
    return object_type == "file" and object_id.startswith("/")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def is_path_pattern(object_type: str, object_id: str) -> bool:
    """Return whether an object id is a supported file path pattern."""
    if not _is_absolute_file_path(object_type, object_id):
        return False
    return object_id in (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX) or object_id.endswith(
        (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX)
    )


def path_pattern_prefix(object_type: str, object_id: str) -> str | None:
    """Return the concrete prefix for a supported path pattern."""
    if not is_path_pattern(object_type, object_id):
        return None
    if object_id in (RECURSIVE_SUFFIX, SINGLE_LEVEL_SUFFIX):
        return "/"
    if object_id.endswith(RECURSIVE_SUFFIX):
        return object_id[: -len(RECURSIVE_SUFFIX)] or "/"
    return object_id[: -len(SINGLE_LEVEL_SUFFIX)] or "/"


def path_pattern_matches(pattern_object_id: str, requested_object_id: str) -> bool:
    """Return whether a pattern object id grants access to a requested path."""
    if not requested_object_id.startswith("/"):
        return pattern_object_id == requested_object_id
    if pattern_object_id == requested_object_id:
        return True
    if pattern_object_id.endswith(RECURSIVE_SUFFIX):
        prefix = (
            "/"
            if pattern_object_id == RECURSIVE_SUFFIX
            else pattern_object_id[: -len(RECURSIVE_SUFFIX)]
        )
        return (
            prefix == "/"
            or requested_object_id == prefix
            or requested_object_id.startswith(prefix + "/")
        )
    if pattern_object_id.endswith(SINGLE_LEVEL_SUFFIX):
        prefix = (
            "/"
            if pattern_object_id == SINGLE_LEVEL_SUFFIX
            else pattern_object_id[: -len(SINGLE_LEVEL_SUFFIX)]
        )
        if prefix == "/":
            remainder = requested_object_id.strip("/")
        elif requested_object_id.startswith(prefix + "/"):
            remainder = requested_object_id[len(prefix) + 1 :]
        else:
            return False
        return bool(remainder) and "/" not in remainder
    return False


def path_pattern_candidates(
    object_type: str,
    object_id: str,
    *,
    include_single_level: bool = True,
) -> list[str]:
    """Return exact and pattern object IDs that can match a requested object."""
    if not _is_absolute_file_path(object_type, object_id):
        return [object_id]

    candidates = [object_id]
    if object_id != "/":
        candidates.append(object_id.rstrip("/") + RECURSIVE_SUFFIX)

    parent = get_parent(object_id)
    if include_single_level and parent is not None:
        candidates.append(SINGLE_LEVEL_SUFFIX if parent == "/" else parent + SINGLE_LEVEL_SUFFIX)

    for ancestor in (*get_ancestors(object_id), "/"):
        recursive = RECURSIVE_SUFFIX if ancestor == "/" else ancestor + RECURSIVE_SUFFIX
        candidates.append(recursive)

    return _dedupe_preserving_order(candidates)
