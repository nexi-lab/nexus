"""Path glob-matching utilities — tier-neutral, zero-kernel-dependency.

Provides ``path_matches_pattern``, a cached glob matcher supporting
``*``, ``**``, and ``?`` wildcards.  Analogous to POSIX ``fnmatch`` /
Linux ``lib/glob.c`` — shared across kernel (``FileEvent``) and
services (reactive subscriptions) without creating cross-tier imports.

Patterns with ``**`` are compiled to regex and cached via ``lru_cache``
for hot-path performance.
"""

import fnmatch
import functools
import re


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

    # Simple patterns without ** use fnmatch
    return fnmatch.fnmatch(path, pattern)
