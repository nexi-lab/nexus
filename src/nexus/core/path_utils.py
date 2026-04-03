"""Path utility functions for Nexus virtual filesystem paths.

Centralizes path normalization, splitting, ancestor/parent computation,
and security validation to eliminate DRY violations across the codebase
(Issue #1293 Item #6, Issue #1287 Decision 5).

All functions are pure and return immutable results (tuples).

# RUST_FALLBACK: path_utils — all functions have Rust equivalents in nexus_fast.
# When Rust is available, every public function delegates to the Rust impl
# (~50ns vs ~1μs Python).  ``grep RUST_FALLBACK src/`` finds all fallback sites.
"""

import functools
import re

# ---------------------------------------------------------------------------
# Rust acceleration (optional — falls back to Python below)
# ---------------------------------------------------------------------------
from nexus._rust_compat import get_ancestors as _rust_get_ancestors
from nexus._rust_compat import get_parent as _rust_get_parent
from nexus._rust_compat import get_parent_chain as _rust_get_parent_chain
from nexus._rust_compat import normalize_path as _rust_normalize_path
from nexus._rust_compat import parent_path as _rust_parent_path
from nexus._rust_compat import path_matches_pattern as _rust_path_matches_pattern
from nexus._rust_compat import split_path as _rust_split_path
from nexus._rust_compat import unscope_internal_path as _rust_unscope_internal_path
from nexus._rust_compat import validate_path as _rust_validate_path
from nexus.contracts.exceptions import InvalidPathError

_RUST_AVAILABLE = _rust_normalize_path is not None

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
    # RUST_FALLBACK: split_path
    if _RUST_AVAILABLE:
        return tuple(_rust_split_path(path))
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
    # RUST_FALLBACK: get_parent
    if _RUST_AVAILABLE:
        return str(_rust_get_parent(path))
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
    # RUST_FALLBACK: get_ancestors
    if _RUST_AVAILABLE:
        return tuple(_rust_get_ancestors(path))
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
    # RUST_FALLBACK: get_parent_chain
    if _RUST_AVAILABLE:
        return tuple((child, parent) for child, parent in _rust_get_parent_chain(path))
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
    # RUST_FALLBACK: validate_path
    if _RUST_AVAILABLE:
        try:
            return str(_rust_validate_path(path, allow_root))
        except ValueError as e:
            raise InvalidPathError(path, str(e)) from None

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
    """Normalize virtual path: absolute, collapse ``//``, resolve ``.`` / ``..``.

    Used by MountTable and PathRouter for canonical path comparison.

    Args:
        path: Absolute virtual path.

    Returns:
        Normalized absolute path.

    Raises:
        ValueError: If path is not absolute or traversal detected.
    """
    # RUST_FALLBACK: normalize_path
    if _RUST_AVAILABLE:
        return str(_rust_normalize_path(path))
    import posixpath

    if not path.startswith("/"):
        raise ValueError(f"Path must be absolute: {path}")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        raise ValueError(f"Path traversal detected: {path}")
    return normalized


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
    # Fast path: no glob metacharacters → exact string comparison.
    if "*" not in pattern and "?" not in pattern:
        return path == pattern
    # RUST_FALLBACK: path_matches_pattern
    # Skip Rust for patterns with non-ASCII chars — the Rust regex crate
    # may reject valid Unicode codepoints that Python re handles fine.
    if _RUST_AVAILABLE and pattern.isascii():
        return bool(_rust_path_matches_pattern(path, pattern))
    compiled = _compile_glob_pattern(pattern)
    if compiled is None:
        return False
    return bool(compiled.match(path))


def parent_path(path: str) -> str | None:
    """Return the parent directory of *path*, or ``None`` for root."""
    # RUST_FALLBACK: parent_path
    if _RUST_AVAILABLE:
        result = _rust_parent_path(path)
        return str(result) if result is not None else None
    if path == "/":
        return None
    path = path.rstrip("/")
    last_slash = path.rfind("/")
    if last_slash == 0:
        return "/"
    return path[:last_slash] if last_slash > 0 else None


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/tenant/user prefix from a storage path."""
    # RUST_FALLBACK: unscope_internal_path
    if _RUST_AVAILABLE:
        return str(_rust_unscope_internal_path(path))
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
