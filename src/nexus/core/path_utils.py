"""Path utility functions for Nexus virtual filesystem paths.

Centralizes path normalization, splitting, and ancestor/parent computation
to eliminate DRY violations across the codebase (Issue #1293, Item #6).

All functions are pure and return immutable results (tuples).
"""

from __future__ import annotations


def split_path(path: str) -> tuple[str, ...]:
    """Split a virtual path into its component parts.

    Args:
        path: Virtual filesystem path (e.g., "/a/b/c.txt")

    Returns:
        Tuple of path components (e.g., ("a", "b", "c.txt"))
        Empty tuple for root path or empty string.
    """
    if not path or path == "/":
        return ()
    return tuple(path.strip("/").split("/"))


def get_parent(path: str) -> str | None:
    """Get the parent directory path.

    Args:
        path: Virtual filesystem path

    Returns:
        Parent path, "/" for root-level items, or None for root itself.

    Examples:
        >>> get_parent("/a/b/c.txt")
        '/a/b'
        >>> get_parent("/a")
        '/'
        >>> get_parent("/")
        None
    """
    parts = split_path(path)
    if not parts:
        return None
    if len(parts) < 2:
        return "/"
    return "/" + "/".join(parts[:-1])


def get_ancestors(path: str) -> tuple[str, ...]:
    """Get all ancestor paths from the path itself down to the shallowest.

    Args:
        path: Virtual filesystem path

    Returns:
        Tuple of ancestor paths from most specific to least specific.
        Does not include root "/".

    Examples:
        >>> get_ancestors("/a/b/c.txt")
        ('/a/b/c.txt', '/a/b', '/a')
        >>> get_ancestors("/a")
        ('/a',)
        >>> get_ancestors("/")
        ()
    """
    parts = split_path(path)
    if not parts:
        return ()
    return tuple("/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1))


def get_parent_chain(path: str) -> tuple[tuple[str, str], ...]:
    """Get (child_path, parent_path) tuples for the full hierarchy.

    Creates the parent chain from leaf to root, useful for building
    directory hierarchy relationships.

    Args:
        path: Virtual filesystem path

    Returns:
        Tuple of (child, parent) pairs from leaf to root.

    Examples:
        >>> get_parent_chain("/a/b/c.txt")
        (('/a/b/c.txt', '/a/b'), ('/a/b', '/a'))
        >>> get_parent_chain("/a")
        ()
    """
    parts = split_path(path)
    if len(parts) < 2:
        return ()
    return tuple(
        ("/" + "/".join(parts[:i]), "/" + "/".join(parts[: i - 1]))
        for i in range(len(parts), 1, -1)
    )
