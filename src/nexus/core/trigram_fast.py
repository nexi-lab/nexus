"""Fast trigram index implementation using Rust acceleration.

This module provides a high-performance trigram-based grep that pre-indexes
file content as trigrams for O(1) index lookup + O(k) candidate verification,
achieving sub-20ms grep on 100K+ files.

Issue #954: Memory-Mapped Trigram Index for Sub-20ms Grep.

Falls back gracefully if Rust extension is not available.
Pattern follows grep_fast.py.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Try to import Rust extension
TRIGRAM_AVAILABLE = False
_build_trigram_index: Callable[..., None] | None = None
_build_trigram_index_from_entries: Callable[..., None] | None = None
_trigram_grep: Callable[..., list[dict[str, Any]]] | None = None
_trigram_search_candidates: Callable[..., list[str]] | None = None
_trigram_index_stats: Callable[..., dict[str, Any]] | None = None
_invalidate_trigram_cache: Callable[..., None] | None = None

try:
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        build_trigram_index as _build_trigram_index,
    )
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        build_trigram_index_from_entries as _build_trigram_index_from_entries,
    )
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        invalidate_trigram_cache as _invalidate_trigram_cache,
    )
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        trigram_grep as _trigram_grep,
    )
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        trigram_index_stats as _trigram_index_stats,
    )
    from nexus._nexus_fast import (  # type: ignore[no-redef]
        trigram_search_candidates as _trigram_search_candidates,
    )

    TRIGRAM_AVAILABLE = True
except ImportError:
    try:
        from nexus_fast import (  # type: ignore[no-redef]
            build_trigram_index as _build_trigram_index,
        )
        from nexus_fast import (  # type: ignore[no-redef]
            build_trigram_index_from_entries as _build_trigram_index_from_entries,
        )
        from nexus_fast import (  # type: ignore[no-redef]
            invalidate_trigram_cache as _invalidate_trigram_cache,
        )
        from nexus_fast import (  # type: ignore[no-redef]
            trigram_grep as _trigram_grep,
        )
        from nexus_fast import (  # type: ignore[no-redef]
            trigram_index_stats as _trigram_index_stats,
        )
        from nexus_fast import (  # type: ignore[no-redef]
            trigram_search_candidates as _trigram_search_candidates,
        )

        TRIGRAM_AVAILABLE = True
    except ImportError:
        pass


def is_available() -> bool:
    """Check if trigram index Rust extension is available."""
    return TRIGRAM_AVAILABLE


def get_index_path(zone_id: str, base_dir: str = "") -> str:
    """Get the expected trigram index file path for a zone.

    Args:
        zone_id: Zone identifier.
        base_dir: Base directory for index storage. Defaults to ~/.nexus/indexes/.

    Returns:
        Absolute path to the zone's trigram index file.
    """
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), ".nexus", "indexes")
    # Sanitize zone_id to prevent path traversal.
    safe_zone_id = os.path.basename(zone_id)
    if not safe_zone_id:
        safe_zone_id = "default"
    return os.path.join(base_dir, f"{safe_zone_id}.trgm")


def index_exists(zone_id: str, base_dir: str = "") -> bool:
    """Check if a trigram index file exists for the given zone.

    Args:
        zone_id: Zone identifier.
        base_dir: Base directory for index storage.

    Returns:
        True if the index file exists.
    """
    return os.path.isfile(get_index_path(zone_id, base_dir))


def build_index(
    file_paths: list[str],
    output_path: str,
) -> bool:
    """Build a trigram index from real filesystem paths.

    The Rust builder reads file content from disk at each path.
    Use build_index_from_entries() when content is already in memory.

    Args:
        file_paths: List of absolute file paths to index.
        output_path: Where to write the index file.

    Returns:
        True on success, False if Rust extension unavailable or error.
    """
    if not TRIGRAM_AVAILABLE or _build_trigram_index is None:
        return False

    try:
        _build_trigram_index(file_paths, output_path)
        return True
    except Exception:
        logger.warning("Failed to build trigram index at %s", output_path, exc_info=True)
        return False


def build_index_from_entries(
    entries: list[tuple[str, bytes]],
    output_path: str,
) -> bool:
    """Build a trigram index from (path, content) pairs without disk I/O.

    Use this when file content is already in memory (e.g., read through
    NexusFS CAS backend). The paths stored in the index are the virtual
    paths, not disk paths.

    Args:
        entries: List of (virtual_path, content_bytes) tuples.
        output_path: Where to write the index file.

    Returns:
        True on success, False if Rust extension unavailable or error.
    """
    if not TRIGRAM_AVAILABLE or _build_trigram_index_from_entries is None:
        return False

    try:
        _build_trigram_index_from_entries(entries, output_path)
        return True
    except Exception:
        logger.warning(
            "Failed to build trigram index from entries at %s", output_path, exc_info=True
        )
        return False


def grep(
    index_path: str,
    pattern: str,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    """Search using trigram index with full file verification.

    Only works when indexed paths are real filesystem paths.
    For NexusFS virtual paths, use search_candidates() + Python verification.

    Args:
        index_path: Path to the trigram index file.
        pattern: Search pattern (literal or regex).
        ignore_case: Whether to ignore case.
        max_results: Maximum number of results.

    Returns:
        List of match dicts with keys: file, line, content, match.
        Returns None if Rust extension unavailable or on error.
    """
    if not TRIGRAM_AVAILABLE or _trigram_grep is None:
        return None

    try:
        result: list[dict[str, Any]] = _trigram_grep(index_path, pattern, ignore_case, max_results)
        return result
    except Exception:
        logger.warning(
            "Trigram grep failed for pattern %r on %s", pattern, index_path, exc_info=True
        )
        return None


def search_candidates(
    index_path: str,
    pattern: str,
    ignore_case: bool = False,
) -> list[str] | None:
    """Get candidate file paths from trigram index without verification.

    Returns file paths that MAY match the pattern based on trigram analysis.
    The caller is responsible for verifying candidates by reading content
    (e.g., through NexusFS).

    Args:
        index_path: Path to the trigram index file.
        pattern: Search pattern (literal or regex).
        ignore_case: Whether to ignore case.

    Returns:
        List of candidate file paths (virtual paths stored during build).
        Returns None if Rust extension unavailable or on error.
    """
    if not TRIGRAM_AVAILABLE or _trigram_search_candidates is None:
        return None

    try:
        return _trigram_search_candidates(index_path, pattern, ignore_case)
    except Exception:
        logger.warning(
            "Trigram candidate search failed for pattern %r on %s",
            pattern,
            index_path,
            exc_info=True,
        )
        return None


def get_stats(index_path: str) -> dict[str, Any] | None:
    """Get statistics about a trigram index.

    Args:
        index_path: Path to the trigram index file.

    Returns:
        Dict with file_count, trigram_count, index_size_bytes.
        Returns None if unavailable or on error.
    """
    if not TRIGRAM_AVAILABLE or _trigram_index_stats is None:
        return None

    try:
        result: dict[str, Any] = _trigram_index_stats(index_path)
        return result
    except Exception:
        logger.warning("Failed to get trigram index stats for %s", index_path, exc_info=True)
        return None


def invalidate_cache(index_path: str) -> None:
    """Invalidate cached trigram index reader for the given path."""
    if TRIGRAM_AVAILABLE and _invalidate_trigram_cache is not None:
        try:
            _invalidate_trigram_cache(index_path)
        except Exception:
            logger.debug("Failed to invalidate trigram cache for %s", index_path, exc_info=True)
