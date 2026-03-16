"""Path utilities — tier-neutral, zero-kernel-dependency.

Provides:
  - ``validate_path``: Validate and normalize a virtual path.
  - ``path_matches_pattern``: Cached glob matcher supporting ``*``,
    ``**``, and ``?`` wildcards.

Analogous to POSIX ``fnmatch`` / Linux ``lib/glob.c`` — shared across
kernel (``FileEvent``), services (reactive subscriptions), and core VFS
without creating cross-tier imports.
"""

from __future__ import annotations

import functools
import re

from nexus.contracts.exceptions import InvalidPathError

# Pre-compiled regex for normalizing consecutive slashes (hot path)
_MULTI_SLASH_RE = re.compile(r"/+")


def validate_path(path: str, *, allow_root: bool = False) -> str:
    """Validate and normalize a virtual path.

    Raises :class:`InvalidPathError` on invalid input.

    This is a **free function** so that both kernel (NexusFS) and services
    can validate paths without importing core.

    Args:
        path: Raw path string to validate.
        allow_root: If *True*, ``"/"`` is accepted (needed for ``list("/")``).

    Returns:
        Normalized path string.

    Raises:
        InvalidPathError: If *path* is empty, contains invalid characters,
            or violates security constraints.
    """
    original_path = path
    path = path.strip() if isinstance(path, str) else path

    if not path:
        raise InvalidPathError(original_path, "Path cannot be empty or whitespace-only")

    if path == "/" and not allow_root:
        raise InvalidPathError(
            "/",
            "Root path '/' not allowed for file operations. Use list('/') for directory listings.",
        )

    if not path.startswith("/"):
        path = "/" + path

    # Normalize multiple consecutive slashes
    path = _MULTI_SLASH_RE.sub("/", path)

    # Remove trailing slash (except for root, but we already rejected that)
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    # Invalid character check (null, newline, carriage return, tab)
    invalid_chars = ["\0", "\n", "\r", "\t"]
    for char in invalid_chars:
        if char in path:
            raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

    # Check for leading/trailing whitespace in path components
    parts = path.split("/")
    for part in parts:
        if part and (part != part.strip()):
            raise InvalidPathError(
                path,
                f"Path component '{part}' has leading/trailing whitespace. "
                f"Path components must not contain spaces at start/end.",
            )

    # Check for parent directory traversal
    if ".." in path:
        raise InvalidPathError(path, "Path contains '..' segments")

    return path


@functools.lru_cache(maxsize=256)
def _compile_glob_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile a glob pattern with ** into a cached regex.

    Cached via lru_cache to avoid recompilation on repeated calls.

    Args:
        pattern: The glob pattern containing **

    Returns:
        Compiled regex pattern, or None if pattern is invalid
    """
    regex_pattern = ""
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            regex_pattern += ".*"  # ** matches anything including /
            i += 2
            # Skip trailing / after **
            if i < len(pattern) and pattern[i] == "/":
                regex_pattern += "/?"
                i += 1
        elif pattern[i] == "*":
            regex_pattern += "[^/]*"  # * matches anything except /
            i += 1
        elif pattern[i] == "?":
            regex_pattern += "."  # ? matches single char
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
    """Check if a path matches a glob pattern.

    Supports:
    - * matches any characters except /
    - ** matches any characters including /
    - ? matches a single character

    Patterns with ** use cached compiled regexes for performance.

    Args:
        path: The file path to check
        pattern: The glob pattern

    Returns:
        True if the path matches the pattern
    """
    if "**" in pattern:
        compiled = _compile_glob_pattern(pattern)
        if compiled is None:
            return False
        return bool(compiled.match(path))

    # Simple patterns also use the regex compiler so * does not cross /
    compiled = _compile_glob_pattern(pattern)
    if compiled is None:
        return False
    return bool(compiled.match(path))


def unscope_internal_path(path: str) -> str:
    """Strip internal zone/tenant/user prefix from a storage path.

    Converts internal storage paths to user-friendly paths by removing
    the zone/tenant and user prefix segments.

    Args:
        path: Internal storage path (e.g., "/tenant:default/workspace/file.txt")

    Returns:
        User-friendly path (e.g., "/workspace/file.txt")

    Examples:
        >>> unscope_internal_path("/tenant:default/connector/gcs/file.txt")
        '/connector/gcs/file.txt'
        >>> unscope_internal_path("/zone/acme/user:alice/workspace/file.txt")
        '/workspace/file.txt'
        >>> unscope_internal_path("/workspace/file.txt")
        '/workspace/file.txt'
    """
    parts = path.lstrip("/").split("/")

    # Determine how many leading segments to skip
    skip = 0

    if parts and parts[0].startswith("tenant:"):
        skip = 1
        if len(parts) > 1 and parts[1].startswith("user:"):
            skip = 2

    elif parts and parts[0] == "zone" and len(parts) >= 2:
        skip = 2  # skip "zone" + zone_id
        if len(parts) > 2 and parts[2].startswith("user:"):
            skip = 3

    if skip == 0:
        return path if path else "/"

    remaining = "/".join(parts[skip:])
    return f"/{remaining}" if remaining else "/"
