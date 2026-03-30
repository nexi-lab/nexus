"""Path utility functions for Nexus virtual filesystem paths.

Centralizes path normalization, splitting, ancestor/parent computation,
and security validation to eliminate DRY violations across the codebase
(Issue #1293 Item #6, Issue #1287 Decision 5).

All functions are pure and return immutable results (tuples).
"""

import functools
import re

from nexus.contracts.exceptions import InvalidPathError

# Pre-compiled regex for normalizing consecutive slashes
_MULTI_SLASH = re.compile(r"/+")

# Characters that must never appear in paths (security)
_INVALID_CHARS = ("\0", "\n", "\r", "\t")


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


# ── Security-enhanced validation (Issue #1287, Decision 5) ─────────────────


def validate_path(path: str, *, allow_root: bool = False) -> str:
    """Validate and normalize a virtual path with security checks.

    SECURITY (v0.7.0): Enhanced validation to prevent cache collisions,
    database issues, and undefined behavior from whitespace and malformed paths.

    Args:
        path: Virtual path to validate.
        allow_root: If True, allow "/" as a valid path (for directory operations).

    Returns:
        Normalized path (stripped, deduplicated slashes, validated).

    Raises:
        InvalidPathError: If path is invalid or malformed.

    Examples:
        >>> validate_path("  /foo/bar  ")
        '/foo/bar'
        >>> validate_path("foo///bar")
        '/foo/bar'
        >>> validate_path(" ")
        Traceback (most recent call last):
            ...
        InvalidPathError: Path cannot be empty or whitespace-only
    """
    original_path = path
    path = path.strip() if isinstance(path, str) else path

    if not path:
        raise InvalidPathError(original_path, "Path cannot be empty or whitespace-only")

    # Reject root "/" for file operations unless explicitly allowed
    if path == "/" and not allow_root:
        raise InvalidPathError(
            "/",
            "Root path '/' not allowed for file operations. Use list('/') for directory listings.",
        )

    # Ensure path starts with /
    if not path.startswith("/"):
        path = "/" + path

    # Normalize multiple consecutive slashes
    path = _MULTI_SLASH.sub("/", path)

    # Remove trailing slash (except for root)
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    # Reject invalid characters (null byte, newline, carriage return, tab)
    for char in _INVALID_CHARS:
        if char in path:
            raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

    # Reject path components with leading/trailing whitespace
    parts = path.split("/")
    for part in parts:
        if part and part != part.strip():
            raise InvalidPathError(
                path,
                f"Path component '{part}' has leading/trailing whitespace. "
                f"Path components must not contain spaces at start/end.",
            )

    # Reject parent directory traversal
    if ".." in path:
        raise InvalidPathError(path, "Path contains '..' segments")

    return path


def normalize_path(path: str) -> str:
    """Lightweight path normalization without security checks.

    Use this for internal paths that are already validated.
    For user-facing paths, always use ``validate_path`` instead.

    Args:
        path: Path to normalize.

    Returns:
        Normalized path with leading slash, no trailing slash.
    """
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


# ── Glob matching + helpers ──────────────────────────────────────────────


@functools.lru_cache(maxsize=256)
def _compile_glob_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile a glob pattern with ** into a cached regex."""
    regex_pattern = ""
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            regex_pattern += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                regex_pattern += "/?"
                i += 1
        elif pattern[i] == "*":
            regex_pattern += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex_pattern += "."
            i += 1
        elif pattern[i] in r"\.[]{}()+^$|":
            regex_pattern += "\\" + pattern[i]
            i += 1
        else:
            regex_pattern += pattern[i]
            i += 1
    try:
        return re.compile("^" + regex_pattern + "$")
    except re.error:
        return None


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Check if *path* matches a glob pattern (``*``, ``**``, ``?``)."""
    compiled = _compile_glob_pattern(pattern)
    if compiled is None:
        return False
    return bool(compiled.match(path))


def parent_path(path: str) -> str | None:
    """Return the parent directory of *path*, or ``None`` for root."""
    if path == "/":
        return None
    path = path.rstrip("/")
    last_slash = path.rfind("/")
    if last_slash == 0:
        return "/"
    return path[:last_slash] if last_slash > 0 else None


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/tenant/user prefix from a storage path."""
    parts = path.lstrip("/").split("/")
    skip = 0
    if parts and parts[0].startswith("tenant:"):
        skip = 1
        if len(parts) > 1 and parts[1].startswith("user:"):
            skip = 2
    elif parts and parts[0] == "zone" and len(parts) >= 2:
        skip = 2
        if len(parts) > 2 and parts[2].startswith("user:"):
            skip = 3
    if skip == 0:
        return path if path else "/"
    remaining = "/".join(parts[skip:])
    return f"/{remaining}" if remaining else "/"
