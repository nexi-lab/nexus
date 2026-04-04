"""Fast grep implementation using Rust acceleration.

This module provides a high-performance grep function that uses the Rust
nexus_kernel library for regex matching, achieving 50-100x speedup over
the pure Python implementation.

Falls back to None if Rust extension is not available.

# RUST_FALLBACK: grep_fast — grep_bulk, grep_files_mmap have Rust equivalents in nexus_kernel.

Issue #893: Added grep_files_mmap for memory-mapped I/O performance.
"""

from typing import Any

# RUST_FALLBACK: grep_bulk, grep_files_mmap
from nexus_kernel import grep_bulk as _rust_grep_bulk
from nexus_kernel import grep_files_mmap as _rust_grep_files_mmap

RUST_AVAILABLE = True
MMAP_AVAILABLE = True


def grep_bulk(
    pattern: str,
    file_contents: dict[str, bytes],
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    """
    Fast bulk grep using Rust.

    Args:
        pattern: Regex pattern to search for
        file_contents: Dict mapping file paths to their content bytes
        ignore_case: Whether to ignore case in pattern matching
        max_results: Maximum number of results to return

    Returns:
        List of match dicts with keys: file, line, content, match
        Returns None if Rust extension is not available

    Each match dict contains:
        - file: File path
        - line: Line number (1-indexed)
        - content: Full line content
        - match: The matched text
    """
    if not RUST_AVAILABLE or _rust_grep_bulk is None:
        return None

    try:
        result: list[dict[str, Any]] = _rust_grep_bulk(
            pattern, file_contents, ignore_case, max_results
        )
        return result
    except (OSError, ValueError, RuntimeError):
        # If Rust grep fails for any reason, return None to fallback to Python
        return None


def is_available() -> bool:
    """Check if Rust grep is available."""
    return RUST_AVAILABLE


def is_mmap_available() -> bool:
    """Check if memory-mapped grep is available."""
    return MMAP_AVAILABLE


def grep_files_mmap(
    pattern: str,
    file_paths: list[str],
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    """
    Fast grep using memory-mapped I/O for zero-copy file access (Issue #893).

    This function reads files directly from disk using mmap, avoiding the overhead
    of passing file contents through Python. Best for searching large local files.

    Performance characteristics:
    - Small files (<4KB): Similar to grep_bulk
    - Medium files (4KB-10MB): 20-40% faster than grep_bulk
    - Large files (>10MB): 50-70% faster than grep_bulk
    - Parallel processing for batches of 10+ files

    Args:
        pattern: Regex pattern or literal string to search for
        file_paths: List of absolute paths to search
        ignore_case: Whether to ignore case in pattern matching
        max_results: Maximum number of results to return

    Returns:
        List of match dicts with keys: file, line, content, match
        Returns None if Rust extension is not available.
        Files that don't exist or can't be read are silently skipped.

    Each match dict contains:
        - file: File path
        - line: Line number (1-indexed)
        - content: Full line content
        - match: The matched text
    """
    if not MMAP_AVAILABLE or _rust_grep_files_mmap is None:
        return None

    try:
        result: list[dict[str, Any]] = _rust_grep_files_mmap(
            pattern, file_paths, ignore_case, max_results
        )
        return result
    except (OSError, ValueError, RuntimeError):
        # If Rust grep fails for any reason, return None to fallback
        return None
